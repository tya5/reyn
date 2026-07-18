---
name: build_and_query_rag_corpus
description: Make a folder of the operator's own documents (txt/md/pdf/xlsx/pptx/docx) searchable by meaning -- ingest them into a user-named sqlite vector store, then query it for the top-k relevant chunks. Read this before running the builtin `rag_ingest` / `rag_query` pipelines, or when the operator asks you to search documents that are NOT already in reyn's own semantic_search index.
---

# Build and query a RAG corpus

Two builtin pipelines do the work; this skill carries what their one-line
descriptions cannot -- **when to reach for them, what order they go in, and
the one mismatch that silently ruins a corpus**.

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

The single `plugin_management__install` call installs **everything the rag plugin
ships** -- both MCP servers (`reyn_chunker` / `reyn_vector_store`), the
`rag_ingest` / `rag_query` pipelines, and this very skill -- in one step: it
copies the plugin to `~/.reyn/plugins/rag/`, materialises its dependencies
(chonkie/apsw/sqlite-vec) into a **dedicated per-plugin environment** (never
reyn's own env), and registers everything into the project's config. **No
`permissions:` block to add** -- a server in the merged config is granted
when the pipeline runs it. The registration step is **probed before it
commits**: a server that does not start is skipped rather than half-writing
config.

`rag_ingest` pre-flights all three servers and returns a **"blocked"**
message naming any unreachable one *before* spending on embeddings -- the
design working, not a crash. When you get one:

- **Not installed yet** (the common case): install it with the call(s)
  above, re-run.
- **Operator refused**: stop and relay it. A refusal is an answer, not an error
  to route around -- **do not shell out, hand-roll an ingest, or re-ask.**
- **Materialisation failed**: `plugin_management__install` reports the failure inline
  (e.g. it could not fetch chonkie/apsw/sqlite-vec) -- **the operator's
  machine or network, not your call**; name what failed and let them.

**Never `pip install markitdown-mcp` beside Reyn** -- it invites a dependency
conflict, and `uvx` fetches it into an isolated environment so it need not
share. If `uvx` cannot reach PyPI (firewalled), use a **separate venv + an
absolute path** -- never Reyn's own venv:

```
python3 -m venv ~/.reyn-markitdown && ~/.reyn-markitdown/bin/pip install markitdown-mcp
mcp__install_local(name="reyn_markitdown", args=[],
                   command="/abs/path/.reyn-markitdown/bin/markitdown-mcp")
```

## Embedding setup -- confirm before you spend on ingest

`rag_ingest` needs a working embedding provider, or every chunk it embeds is
wasted spend against a call that was never going to succeed. Unless the
resolved model carries the `sentence-transformers/` prefix (a separate,
in-process backend -- see the next section), **every embedding call in reyn
routes through `litellm`**: straight to the provider's own API, or through a
**litellm proxy** if the env var `LITELLM_API_BASE` is set (the same variable
`call_llm` reads -- one proxy serves both). Default classes: `light` /
`standard` -> `openai/text-embedding-3-small`, `strong` ->
`openai/text-embedding-3-large`.

### Pre-flight: confirm the endpoint actually answers (do this before `rag_ingest`)

One curl, before you spend anything on an ingest. This check is
**transport-independent by construction**: reyn always sends embedding
requests to an OpenAI-compatible `/embeddings` endpoint at whatever
`LITELLM_API_BASE` names -- a litellm proxy, a direct embedding API, or a
local server all look the same from reyn's side, so the same one-liner
verifies any of them:

```bash
curl -s "${LITELLM_API_BASE:-<your-endpoint>}/embeddings" \
  -H "Authorization: Bearer ${OPENAI_API_KEY:-dummy}" \
  -H "Content-Type: application/json" \
  -d '{"model": "<the model name your endpoint expects>", "input": "hello"}' \
  | jq '.data[0].embedding | length'
```

Replace `<your-endpoint>` / the model name / the key with your actual
values -- this is a shape to adapt, not a literal command. **Healthy**:
prints a positive integer (the embedding dimension, e.g. `1536`) --
`data[0].embedding` came back as a non-empty float array. Typical failure
signatures:

- **401** -- wrong or missing API key.
- **404 / "model not found"** -- that model name isn't registered at this
  endpoint (proxy `model_list` mismatch, or a wrong direct-API model
  string).
- **400, unsupported param** -- *only relevant when routing through a
  proxy* (see Case B): the proxy is missing
  `litellm_settings.drop_params: true` (#1616).
- **connection refused** -- nothing is listening at that endpoint, or
  `LITELLM_API_BASE` points at the wrong address.

### Case A -- you have an embedding API key -- no proxy needed

This is the shortest path, and it does **not** go through a proxy at all:
`reyn secret set OPENAI_API_KEY` (or your provider's key), and stop --
that's it. With `LITELLM_API_BASE` unset, reyn's litellm client calls the
provider's API **directly** (`_proxy_kwargs()` returns nothing when the env
var is absent), so the default `standard` class
(`openai/text-embedding-3-small`) works with **no `reyn.yaml` edit, no
`LITELLM_API_BASE`, no proxy, and no `drop_params` setting** -- the client
already passes `drop_params=True` on every call, which is only a no-op when
a proxy sits in between (see Case B). Run the pre-flight curl above against
the provider's own endpoint (e.g. `https://api.openai.com/v1`) to confirm.

If your organization already routes LLM traffic through a shared litellm
proxy, you're effectively in the Case B situation below (proxy in the
path) even though you have a key -- the proxy's `drop_params` note applies
to you too.

### Case B -- no embedding API contract -> litellm proxy + a local model

No key, and you don't want one: run a local embedding model behind a
litellm proxy. The proxy is what turns that local model into the
OpenAI-compatible endpoint reyn already expects -- reyn itself never talks
to the local server directly.

**Step 1 -- start a local embedding server.** Ollama is the lightest setup
(openai-compatible embeddings out of the box); reference commands below,
**verify on your own machine, versions/ports may differ**:

```bash
ollama pull nomic-embed-text
ollama serve   # if not already running as a background service
curl http://localhost:11434/api/embeddings -d '{"model": "nomic-embed-text", "prompt": "hello"}'
```

(Alternatives, one line each: HuggingFace `text-embeddings-inference`, or
`infinity` -- both also expose an OpenAI-compatible embeddings endpoint.)

**Step 2 -- register it in the litellm proxy's `config.yaml`.** Syntax
confirmed against litellm's own docs
(https://docs.litellm.ai/docs/proxy/embedding,
https://docs.litellm.ai/docs/proxy/configs, checked 2026-07):

```yaml
model_list:
  - model_name: text-embedding-3-small   # see the naming rule below
    litellm_params:
      model: ollama/nomic-embed-text
      api_base: http://localhost:11434

litellm_settings:
  drop_params: true   # required -- see the 400 failure signature above (#1616)
```

Restart the proxy after editing.

**Step 3 -- point reyn at the proxy:**

```bash
export LITELLM_API_BASE=http://localhost:4000   # your proxy's address
```

**Naming rule (read before choosing a model name):** when
`LITELLM_API_BASE` is set, reyn strips the resolved model string's leading
`provider/` segment before sending it to the proxy -- `openai/foo` arrives
at the proxy as plain `foo`. So the proxy's `model_list[].model_name` must
equal **everything after the first `/`** of whichever reyn-side model
string you use:

- **(a) Simplest -- no `reyn.yaml` edit at all.** Keep using the default
  `standard`/`light` class (`openai/text-embedding-3-small`). Register the
  proxy's `model_name` as `text-embedding-3-small` (as in Step 2 above) --
  the local model now answers under reyn's default class name.
- **(b) Or add an explicit class**, e.g. in `reyn.yaml`:
  ```yaml
  embedding:
    classes:
      local:
        model: openai/nomic-embed-text
  ```
  Here the proxy's `model_name` must be `nomic-embed-text` (everything
  after `openai/`), and you'd pass `embedding_model: "local"` to
  `rag_ingest.ingest` / `rag_query.query`.

**Step 4 -- confirm it end to end.** Re-run the pre-flight curl above
first (cheapest check). Then run a real ingest + query and confirm a chunk
actually comes back:

```
pipeline__run(name="rag_ingest.ingest", input={
  "input_path": "/abs/path/to/docs", "output_db": "./rag/docs.sqlite",
})
pipeline__run(name="rag_query.query", input={
  "query_text": "<something you know is in the docs>",
  "db": "./rag/docs.sqlite", "top_k": 3,
})
```

A non-empty `[{id, distance, metadata}, ...]` list is the real signal --
`chunks_upserted > 0` on the ingest response alone does not prove the
vectors are meaningful. Typical failure at this step: an empty query
result with a populated db usually means a Case B naming mismatch (Step 3);
`rag_ingest` returning "blocked" means a server, not the embedding
endpoint, is unreachable -- see "Prerequisites" above.

This section covers the **embedding provider** only -- a separate concern
from the vector store / chunker / parser servers. See "Swapping the
backend" below to change those. For local-model tradeoffs against an API-backed class
(cost, latency, offline use), see
`docs/guide/for-users/enable-semantic-search.md` -- written for reyn's
*other* RAG (`semantic_search`) but the embedding-provider tradeoffs it
walks through are the same ones this section's Case A/B choice makes.

## The workflow

Both steps below run through `pipeline__run` -- the launch verb for a
**REGISTERED** pipeline, invoked by the name it was installed under
(`"rag_ingest.ingest"` / `"rag_query.query"`). This is **not**
`pipeline__run_inline`, which takes an inline DSL definition instead of a
name and is a different tool for a different purpose (defining a pipeline
on the fly) -- passing `rag_ingest.ingest`'s `name`/`input` shape to
`pipeline__run_inline` fails; it expects a `pipeline` body, not a `name`.

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
"unknown", never as free.**

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

**Want the store outside cwd?** A **declared deviation, not the default**: the
operator must add a `write_paths` entry for that location to
`reyn_vector_store`'s config (see `docs/cookbook/configs/with-builtin-rag-mcp.yaml`);
without it the sandbox denies the write. **Do not pass an absolute
`output_db`/`db` unless they already have.** Unlike the server entry,
`write_paths` is **not** something `mcp__install_local` can set -- relay that
denial (it names the sandbox, path, and knob) rather than retrying. The
alternative you can act on: a cwd-relative `output_db`.

Returns `[{id, distance, metadata}, ...]`, **nearest first**. `metadata`
carries `source_path` / `chunk_index` / `content_hash` / `embedding_model`.
**It does not carry the chunk text** -- the store has no column for it. To
quote a hit, read `metadata.source_path` with the ordinary file read op.

## ⚠️ One sqlite file = one embedding model

`embedding_model` defaults to `"standard"` on **both** pipelines. If you pass
it to one, **pass the same value to the other** -- a mismatch changes the
vector space, and the query either raises `VectorDimensionMismatchError` or,
at the same dimension, returns **quietly meaningless neighbours**. Different
model -> different sqlite file: to re-embed, ingest into a **new** `output_db`;
pointing a new model at the old file corrupts it.

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
first, same as above):

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
