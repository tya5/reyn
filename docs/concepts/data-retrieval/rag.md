---
type: concept
topic: rag
audience: [human, agent]
---

# RAG (Retrieval-Augmented Generation)

reyn ships an internal RAG **framework foundation** — the `embed` / `index_query` / `index_drop` / `semantic_search` / `index_update` control-IR ops, an extensible `IndexBackend` protocol, an `EmbeddingProvider` protocol, and a safe-mode `index_update()` entry point — that lets you index any document corpus into reyn's own store. **As of FP-0066 P1b, this in-core index is OS-internal**: the four ops that used to be agent-facing tools (`semantic_search`, `index_update`, `drop_source`, `list_rag_sources`) are retired as LLM-callable tools. They were a pre-audience-split relic — user-RAG semantics riding reyn's own internal store, from before user RAG and in-core RAG were split into separate systems. See [proposal 0066 §9](../../deep-dives/proposals/0066-retrieval-two-groups-two-axes.md) for the retire rationale and the later phases that will make the in-core index reachable again (as `search_actions` / a future `search_knowledge` verb — not a general-purpose agent search tool).

> **If you want an agent to search your own documents today, use the builtin user RAG** (proposal 0063): two bundled pipelines that ingest a folder of documents (pdf / xlsx / pptx / docx / txt / md) into **an external sqlite vector store you name**, via MCP servers, and query it — no Python step to write, and it is agent-callable end-to-end. See [Build a RAG corpus](../../guide/for-users/build-a-rag-corpus.md). That is a *different* store with a *different* setup from the in-core index this page describes; the two share only the `embed` primitive and the `embedding:` class config below.

## The in-core index is OS-internal

`index_update()` (safe-mode, `reyn.api.safe.index_update`) still lets you build a source in reyn's own store — the reconcile / chunking contract below is unchanged. What changed is what can READ that store: there is currently no agent-facing tool and no safe-mode search entry point over the in-core index. The substrate (`IndexUpdateIROp` / `SemanticSearchIROp` / `SqliteIndexBackend`) is kept because later FP-0066 phases (§8 ingest, §5 search) build reyn's own internal retrieval on top of it — action-catalog search (`search_actions`) already does; skill/memory/repo retrieval (a future `search_knowledge` verb) is planned. None of that is a general-purpose "index your own docs and have the LLM search them" surface — for that, use the FP-0063 plugin above.

```python
# my_project/index_docs.py — run once via a `python` step (mode: safe by default)
from reyn.api.safe import file, index_update as iu

paths = file.glob("docs/**/*.md")
chunks = []
for path in paths:
    text = file.read(path)
    # naive paragraph split — replace with whatever chunking suits your corpus
    for i, para in enumerate(text.split("\n\n")):
        if not para.strip():
            continue
        chunks.append({
            "text": para,
            "metadata": {"content_hash": f"{path}:{i}", "source_path": path},
        })

iu.index_update(
    chunks,
    source="my_docs",
    model="text-embedding-3-small",
    description="Project documentation",
    path="docs/**/*.md",
)
```

`index_update` is a **reconcile**, not an append/replace toggle — see [§Chunking is your own code](#chunking-is-your-own-code) and [§Limitations](#limitations) for the add/update/remove/skip contract (a re-run with the same chunks re-embeds nothing; a re-run with a changed `content_hash` under an already-indexed `source_path` re-embeds just that chunk and drops the stale one).

## What is a "source"

A **source** is a named collection of chunks from a set of files. You give it:

| Field | Example | Purpose |
|-------|---------|---------|
| `source` | `my_docs` | Logical name for this collection of chunks |
| `path` | `docs/**/*.md` | Single glob pattern — all matching files are indexed together |
| `description` | `"Project documentation"` | Required. Free-text label for the source |

One indexing run covers one source, one path, one chunking approach. Source metadata is persisted in `.reyn/config/index/sources.yaml`.

## Chunking is your own code

There is no bundled chunker and no LLM-driven strategy selection — the chunking logic in the example above (paragraph split) is plain Python you write and adapt per corpus. For specialised corpora — Python source code, SQL schemas, structured YAML — swap in whatever splitting logic fits (e.g. an AST-based splitter for source code, a heading-based splitter for Markdown) before calling `index_update`.

The chunking step runs deterministically in your `python` step — no LLM involvement, no attractor surface. `index_update` handles the add/update/remove/skip reconciliation, embedding, and index writes; everything upstream of that call (reading files, splitting into chunks) is ordinary Python. Pass the **full current chunk set** for whatever `source_path`s you are (re-)indexing in one call — reconciliation needs to see the complete set for a path to detect deletions (a partial re-ingest that only ever mentions a few files never mass-deletes the rest of the source).

## Storage location

All index data is stored inside your project's `.reyn/` directory:

```
.reyn/
  config/
    index/
      sources.yaml                 # Source manifest — name, path, model, chunk count
  cache/
    index/
      my_docs/
        index.db                   # SQLite vector store for this source
      memory/
        index.db
```

`sources.yaml` is the single source of truth for what is indexed; it lives under `config/` because it is operator-editable state. The SQLite index data lives under `cache/` because it is derived/rebuildable. See [`.reyn/` directory layout](../../reference/runtime/reyn-dir-layout.md) for the full recovery-core/cache/audit split. The SQLite files contain the chunk text and embedding vectors. You can inspect them with any SQLite client, though the schema is internal.

## Cost

Embedding cost is linear in to-embed chunk count (after the add/update dedup — unchanged chunks are skipped, never re-embedded) and depends on your corpus size and embedding model — `text-embedding-3-small` is the default. A large to-embed batch (over `embedding.cost_warn_threshold`, see [§Embedding configuration](#embedding-configuration)) surfaces an `index_update_cost_warning` audit-event and a `cost_warning` field in the returned envelope — check `result["cost_warning"]` if you want to react to it in your indexing script.

## Embedding configuration

The embedding model and batching behaviour are configured under `embedding:` in `reyn.yaml`. Three built-in classes ship by default, all OpenAI-backed:

```yaml
embedding:
  enabled: true
  default_class: standard
  classes:
    light:      openai/text-embedding-3-small
    standard:   openai/text-embedding-3-small
    strong:     openai/text-embedding-3-large
  batch_size: 100
  max_retries: 3
  timeout: 60.0
  cost_warn_threshold: 10000
```

`embedding.enabled` (default `false`, opt-in) gates the embed op and everything built on it — see [proposal 0066 §7](../../deep-dives/proposals/0066-retrieval-two-groups-two-axes.md#7-opt-in-embeddingenabled-symmetric-model).

`timeout` is the per-attempt deadline (seconds) — how long reyn waits for one embedding attempt. It exists because a stalled embedding endpoint would otherwise be capped only by litellm's own `request_timeout` default of 6000s per attempt, which an operator cannot tell from a hang. `<= 0` opts out.

**It is not a cost control.** `timeout` bounds waiting, not sending: the OpenAI SDK client retries beneath it, so one attempt can put up to 3 requests on the wire and `max_retries: 3` up to 9 — all 9 measured delivered in ~7.6s under the default 60.0s bound, which never engages. Lowering `timeout` does not reduce what the provider computes. See [reyn.yaml § `embedding` fields](../../reference/config/reyn-yaml.md#embedding-fields) and [#3047](https://github.com/tya5/reyn/issues/3047).

Reyn depends on litellm **exclusively** for embeddings — there is no in-process model backend (#3128 removed the sentence-transformers-backed `local-mini` / `local-e5` classes that shipped under FP-0043). Every class's `model` string is a LiteLLM-routable name; dispatch goes straight through LiteLLM to the provider's own API, or through a **litellm proxy** when the `LITELLM_API_BASE` env var is set (the same variable `call_llm` reads).

The OpenAI API key is read from `~/.reyn/secrets.env` via `${OPENAI_API_KEY}` — no literal value in `reyn.yaml`. After setting the key with `reyn secret set OPENAI_API_KEY`, indexing with `standard` / `light` / `strong` works out of the box with no further configuration.

### Local and offline embedding models

Reyn ships no in-process local-embedding backend. An operator who wants a local model (no API key, or an offline/air-gapped setup) runs it behind a **litellm proxy** and points reyn at it — the proxy is what turns a local server (Ollama / HuggingFace `text-embeddings-inference` / `infinity`) into the OpenAI-compatible endpoint reyn already expects; reyn itself never talks to the local server directly. Add an entry under `embedding.classes` pointing at the proxied model, e.g.:

```yaml
embedding:
  classes:
    local:
      model: openai/nomic-embed-text   # name after LITELLM_API_BASE strips the provider/ prefix
```

then `export LITELLM_API_BASE=http://localhost:4000` (your proxy's address) before starting reyn. Full setup walkthrough (server choice, proxy `config.yaml`, the `provider/` name-stripping rule, pre-flight verification) lives in [Guide: enable semantic search § Case B](../../guide/for-users/enable-semantic-search.md#case-b-no-embedding-api-contract-litellm-proxy-a-local-model) — written for `search_actions` but the same mechanism serves `index_update`.

For chat-side action retrieval specifically (= `search_actions`), see [Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md) and the [`reyn embeddings`](../../reference/cli/embeddings.md) CLI for cache management.

## Permissions

There is no dedicated permission gate on `index_update()` itself — a safe-mode `python` step that calls it runs under the calling phase's ordinary python-step permissions, not a RAG-specific one.

## Phase history

**FP-0066 P1b (this state)**: the agent-facing layer-1 tools (`semantic_search`, `index_update`, `drop_source`, `list_rag_sources`) are retired. In-core indexing (`index_update()` safe-mode) still works; there is currently no agent-facing or safe-mode search entry point over the result. See [proposal 0066](../../deep-dives/proposals/0066-retrieval-two-groups-two-axes.md) for the phases that will make the in-core index reachable again.

**Landed pre-retirement (historical):**

- **FP-0043** added the local-embedding path for chat-side action retrieval (`search_actions`). It originally shipped as an in-process `sentence-transformers` backend; **#3128 removed that in-process backend** — reyn depends on litellm exclusively for embeddings now, and "local" is reached, if wanted, via an operator-run litellm proxy fronting a local model server — see [§Local and offline embedding models](#local-and-offline-embedding-models).
- **FP-0057 Phase 2a/2b**: `recall` renamed `semantic_search` (later retired, FP-0066 P1b); the safe-mode ingestion entry point is `index_update()` (`reyn.api.safe.index_update`) — an incremental/delta-reconcile call (add/update/remove/skip against the source's current index), replacing the retired `embed_and_index()` (`reyn.api.safe.embed_index`, clean-break, no shim). This closes the "no incremental indexing" gap — reconcile detects deleted/changed source files by content_hash, no separate rebuild mode needed for ordinary file changes.
- **#3026** added `list_rag_sources` (later retired, FP-0066 P1b) as the discovery verb naming indexed corpora.

**Deferred to a future phase:**

- Alternative vector store backends (Qdrant, FAISS, Pinecone)
- Advanced retrieval (rerank, HyDE, contextual retrieval)
- Additional local backends (ollama, ONNX, GGUF)
- RAG evaluation framework
- Reachable in-core search (`search_knowledge`, per proposal 0066 §5/§11 P3)

## Limitations

- **100K chunks recommended maximum** per source for the SQLite backend. Larger corpora will work but query latency increases.
- **No full-rebuild mode.** `index_update` is reconcile-only (add/update/remove/skip against the current index) — there is no `mode="replace"` full-clear-and-rebuild call. To force a from-scratch rebuild, drop the source's index data directly (there is no agent-facing `drop_source` tool any more) and re-run `index_update` against the empty source.
- **No agent-facing or safe-mode search entry point.** As of FP-0066 P1b, indexing via `index_update()` builds the store, but nothing reads it back except later OS-internal phases (`search_actions` today; a future `search_knowledge`). Use the FP-0063 user RAG plugin if you need agent-driven search over your own documents today.
- **No advanced retrieval.** Cosine similarity only — no reranking, HyDE, or contextual retrieval.
- **Sensitive data.** reyn does not redact sensitive content before indexing. Do not index secrets, credentials, or PII unless you understand the implications.
- **Embedding requires either an API key OR a self-run litellm proxy.** The built-in classes (`light` / `standard` / `strong`) need `OPENAI_API_KEY`; a fully credential-free path needs an operator to stand up a local embedding server behind a litellm proxy and add an `embedding.classes` entry pointing at it (see [§Local and offline embedding models](#local-and-offline-embedding-models)). See [§Embedding configuration](#embedding-configuration).

## See also

- [Guide: Build a RAG corpus](../../guide/for-users/build-a-rag-corpus.md) — the agent-callable user RAG: the builtin pipelines over an external sqlite store (proposal 0063)
- [Proposal 0066: retrieval redesign](../../deep-dives/proposals/0066-retrieval-two-groups-two-axes.md) — why the in-core agent tools were retired, and what replaces them
- [ADR-0033](../../deep-dives/decisions/0033-rag-extensible-os.md) — design rationale and full technical spec (internal, historical)
- [Concepts: workspace](../runtime/workspace.md) — how `.reyn/` state is structured
- [Concepts: secret handling](../runtime/secret-handling.md) — embedding API key management
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — `embedding:` section schema
