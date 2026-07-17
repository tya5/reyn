---
type: concept
topic: rag
audience: [human, agent]
---

# RAG (Retrieval-Augmented Generation)

reyn ships a RAG **framework foundation** — five primitive ops (`embed` / `index_query` / `index_drop` / `semantic_search` / `index_update`), an extensible `IndexBackend` protocol, an `EmbeddingProvider` protocol, and a safe-mode `index_update()` entry point — that lets you index any document corpus and have the LLM retrieve relevant chunks at query time, without ever overloading the context window with the full corpus.

**The differentiation: retrieval is a built-in tool, not a library call.** LangChain and LlamaIndex give you a Python pipeline you call from your own driver code. reyn's `semantic_search` and `drop_source` are built-in tools the LLM itself calls during a normal `reyn chat` session — no orchestration code required on the search side.

**Phase 1 scope (= 1.0 release).** The framework foundation, the SQLite default backend (≤100K chunks, sub-second query), and the LiteLLM embedding passthrough ship in 1.0. Vector store plugin variety (Qdrant / FAISS / Weaviate / Pinecone), advanced retrieval (rerank / HyDE / contextual retrieval), RAG eval frameworks, and IDE integration are post-1.0 (= phase 2) territory — see [../architecture/care-boundary.md](../architecture/care-boundary.md). If you need that ecosystem today, LangChain / LlamaIndex are the better fit.

**TL;DR:** Search is automatic — the LLM calls the built-in `semantic_search` tool whenever it needs information from an indexed source. Creating a source requires a short safe-mode Python step that reads your files and calls `index_update()` (there is no bundled one-command indexer for an **in-core source**).

> **This page is about the in-core RAG.** Reyn also ships a **builtin user RAG** (proposal 0063): two bundled pipelines that ingest a folder of documents (pdf / xlsx / pptx / docx / txt / md) into **an external sqlite vector store you name**, via MCP servers, and query it — no Python step to write. It is a *different* store with a *different* setup, and it does **not** create a source `semantic_search` can see. Use this page's `IndexBackend` path when you want reyn's own index; see [Build a RAG corpus](../../guide/for-users/build-a-rag-corpus.md) when you want a portable store of your own documents. The two share only the `embed` primitive and the `embedding:` class config below.

## Quick start

Indexing a corpus is a small script run once as a safe-mode `python` step — read the files, split them into chunks, hand them to `index_update`:

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

```bash
# Start chatting — the LLM will fetch chunks via semantic_search when needed
reyn chat
> Summarise the authentication design from the docs
```

Verified end-to-end with real `gemini-embedding-001` via the LiteLLM proxy: 21 EN concept docs → 418 chunks indexed (~$0.001), and natural concept queries ("What is X in Reyn?", "Explain Reyn's permission model") returned the indexed semantic answers in 3/3 chat runs (= batch 22, 2026-05-10). See `docs/deep-dives/journal/dogfood/2026-05-10-batch-22-affordance-bias-fix/findings.md`. (That run predates both the `embed_and_index()` entry point and its FP-0057 Phase 2b successor `index_update()`, and used the since-removed `index_docs` skill — the underlying embed/index/search mechanics are unchanged; `recall` was renamed `semantic_search` in FP-0057 Phase 2a.)

Behind the scenes the LLM calls `semantic_search` and retrieves the top matching chunks:

```
LLM internally calls: semantic_search(query="authentication design", sources=["my_docs"], top_k=5)
```

The same script pattern indexes any file glob — user notes, source code, or JSONL logs — just point `file.glob()` at a different path and pick a `source` name.

## What is a "source"

A **source** is a named collection of chunks from a set of files. You give it:

| Field | Example | Purpose |
|-------|---------|---------|
| `source` | `my_docs` | Logical name used in `semantic_search` calls and `reyn source` commands |
| `path` | `docs/**/*.md` | Single glob pattern — all matching files are indexed together |
| `description` | `"Project documentation"` | Required. Helps the LLM decide when to search this source |

One indexing run covers one source, one path, one chunking approach. To index multiple file types with different chunking, run the indexing script once per source and then combine them at query time using `sources=[...]`:

```
semantic_search(query="...", sources=["python_src", "my_docs", "memory"], top_k=5)
```

Source metadata is persisted in `.reyn/index/sources.yaml`. The LLM discovers what
is indexed by calling `list_rag_sources`, which returns each source's name,
description, and chunk count:

```
list_rag_sources()
→ {"sources": [
    {"name": "memory",    "description": "User notes / past session memos", "chunk_count": 142},
    {"name": "reyn_code", "description": "Reyn Python framework code",      "chunk_count": 1247},
    {"name": "my_docs",   "description": "Project documentation",           "chunk_count": 89}
  ]}
```

Those names are what `semantic_search`'s `sources` argument takes. Discovery is a
tool call rather than a standing block in the system prompt, so an operator with
many corpora pays for the list only on the turns the model actually asks.

## The `semantic_search` tool

`semantic_search` is a built-in tool available to the LLM in every chat session (FP-0057 Phase 2a; renamed from `recall` — clean-break, fixes the observed recall/search_actions/memory naming collision). It takes a natural-language query, searches the requested sources, and returns the top-K matching chunks:

```
semantic_search(query="plan-mode discussion", sources=["memory"], top_k=5)
```

The LLM picks which sources to search based on the source descriptions you provided at index time. You do not need to configure which sources a workflow may use — any indexed source is accessible.

Internally, `semantic_search` embeds the query using the same model used for indexing (once per DISTINCT model when sources span more than one — never a caller-supplied model per source), runs a cosine-similarity search against each source's SQLite index, and merges results ranked by similarity score. The entire operation is deterministic; the LLM sees only the top-K chunks as text, never the raw vectors.

A second built-in tool, `drop_source`, lets the LLM drop an index on your behalf — useful when iterating on a chunking strategy:

```
drop_source(source="my_docs")
```

## Chunking is your own code

There is no bundled chunker and no LLM-driven strategy selection — the chunking logic in the [Quick start](#quick-start) example (paragraph split) is plain Python you write and adapt per corpus. For specialised corpora — Python source code, SQL schemas, structured YAML — swap in whatever splitting logic fits (e.g. an AST-based splitter for source code, a heading-based splitter for Markdown) before calling `index_update`.

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

Phase 1 uses SQLite as the only storage backend. Phase 2 will add pluggable backends (Qdrant, FAISS, Pinecone) via a `register_backend()` extension point.

## Permissions

One permission gate protects RAG operations on the LLM-facing side:

| Permission | Default | Trigger |
|-----------|---------|---------|
| `permissions.index_drop` | `ask` | `drop_source` tool call or `reyn source rm` |

There is no dedicated permission gate on `index_update()` itself — a safe-mode `python` step that calls it runs under the calling phase's ordinary python-step permissions, not a RAG-specific one.

## Cost

Embedding cost is linear in to-embed chunk count (after the add/update dedup — unchanged chunks are skipped, never re-embedded) and depends on your corpus size and embedding model — `text-embedding-3-small` is the default. Unlike the removed `index_docs` skill's wrapper, the safe-mode entry has no interactive cost preflight, but a large to-embed batch (over `embedding.cost_warn_threshold`, see [§Embedding configuration](#embedding-configuration)) now surfaces an `index_update_cost_warning` audit-event and a `cost_warning` field in the returned envelope — check `result["cost_warning"]` if you want to react to it in your indexing script.

## Embedding configuration

The embedding model and batching behaviour are configured under `embedding:` in `reyn.yaml`. Five built-in classes ship by default — three OpenAI-backed and two sentence-transformers-backed (= local; activated by the `local-embed` extras, see [§Local embedding backend](#local-embedding-backend-fp-0043) below):

```yaml
embedding:
  default_class: standard
  classes:
    light:      openai/text-embedding-3-small
    standard:   openai/text-embedding-3-small
    strong:     openai/text-embedding-3-large
    local-mini: sentence-transformers/all-MiniLM-L6-v2
    local-e5:   sentence-transformers/intfloat/multilingual-e5-small
  batch_size: 100
  max_retries: 3
  timeout: 60.0
  cost_warn_threshold: 10000
```

`timeout` is the per-attempt deadline (seconds) — how long reyn waits for one embedding attempt. It exists because a stalled embedding endpoint would otherwise be capped only by litellm's own `request_timeout` default of 6000s per attempt, which an operator cannot tell from a hang. `<= 0` opts out.

**It is not a cost control.** `timeout` bounds waiting, not sending: the OpenAI SDK client retries beneath it, so one attempt can put up to 3 requests on the wire and `max_retries: 3` up to 9 — all 9 measured delivered in ~7.6s under the default 60.0s bound, which never engages. Lowering `timeout` does not reduce what the provider computes. See [reyn.yaml § `embedding` fields](../../reference/config/reyn-yaml.md#embedding-fields) and [#3047](https://github.com/tya5/reyn/issues/3047).

Dispatch is **provider-prefix-based**: classes whose `model` string starts with `sentence-transformers/` route to the local backend; everything else (`openai/`, future LiteLLM-routable providers) routes through LiteLLM. Existing OpenAI-backed callers are byte-identical to pre-FP-0043; the routing wrapper passes them through transparently.

The OpenAI API key is read from `~/.reyn/secrets.env` via `${OPENAI_API_KEY}` — no literal value in `reyn.yaml`. After setting the key with `reyn secret set OPENAI_API_KEY`, indexing with `standard` / `light` / `strong` works out of the box with no further configuration.

### Local embedding backend (FP-0043)

`local-mini` and `local-e5` use [sentence-transformers](https://www.sbert.net/) to embed locally (= no API, no credentials, no per-query cost). They are gated behind an `extras` install so the base `reyn` package stays small:

```bash
pip install 'reyn[local-embed]'
```

This pulls `sentence-transformers >= 2.7` and `torch >= 2.0`. The model itself downloads on first use (~22 MB for `local-mini`, ~118 MB for `local-e5`) and caches under `~/.cache/reyn/sentence-transformers/` (overridable via `REYN_CACHE_DIR` / `XDG_CACHE_HOME`).

Device selection is `cpu` by default; opt into GPU acceleration via the `REYN_EMBED_DEVICE` env var (`mps` for Apple Silicon, `cuda` for NVIDIA). Invalid values warn and fall back to `cpu`.

On an air-gapped / firewalled network where Hugging Face is unreachable, set the HuggingFace-standard `HF_HUB_OFFLINE=1` (or `TRANSFORMERS_OFFLINE=1`) (FP-0057 Phase 4) — reyn respects the ecosystem-standard offline flag (rather than a reyn-native knob) and passes `local_files_only=True` explicitly, so an uncached model then fails fast and deterministically instead of hanging on a connect timeout. This is always an explicit operator opt-in; reyn never infers offline-ness or silently falls back to an API-backed embedding class. See [Guide § Offline / air-gapped networks](../../guide/for-users/enable-semantic-search.md#offline--air-gapped-networks) for the full preload-and-copy-cache walkthrough.

For chat-side action retrieval specifically (= `search_actions`), see [Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md) and the [`reyn embeddings`](../../reference/cli/embeddings.md) CLI for cache management.

## Phase 1 scope

**Included in Phase 1 (1.0 release):**

- `semantic_search` tool available to the LLM in every chat session
- `drop_source` tool for cleanup
- SQLite vector store backend
- `reyn source list / describe / rm` CLI
- Empty-state hint in the chat system prompt

**Deferred to Phase 1.5 (1.1+):**

- Memory layer migration from inline expansion to `semantic_search(sources=["memory"])`. Memory continues to work as-is in 1.0.

**Landed post-1.0:**

- Local embedding models via sentence-transformers (FP-0043) — see [§Local embedding backend](#local-embedding-backend-fp-0043). The chat-side `search_actions` surface is the first consumer; the same `local-mini` / `local-e5` classes are reachable from `embedding.default_class` for document indexing too.
- **FP-0057 Phase 2a/2b**: `recall` renamed `semantic_search`; the safe-mode ingestion entry point is now `index_update()` (`reyn.api.safe.index_update`) — an incremental/delta-reconcile call (add/update/remove/skip against the source's current index), replacing the retired `embed_and_index()` (`reyn.api.safe.embed_index`, clean-break, no shim). This also closes the "no incremental indexing" gap below — reconcile detects deleted/changed source files by content_hash, no separate rebuild mode needed for ordinary file changes.

**Deferred to Phase 2 (post-1.1):**

- Alternative vector store backends (Qdrant, FAISS, Pinecone)
- Advanced retrieval (rerank, HyDE, contextual retrieval)
- Additional local backends (ollama, ONNX, GGUF)
- RAG evaluation framework

## Limitations

- **100K chunks recommended maximum** per source for Phase 1 SQLite backend. Larger corpora will work but query latency increases.
- **No full-rebuild mode.** `index_update` is reconcile-only (add/update/remove/skip against the current index) — there is no `mode="replace"` full-clear-and-rebuild call. To force a from-scratch rebuild, call the `index_drop` tool (or `reyn source rm`) on the source first, then re-run `index_update` on the empty source.
- **Memory layer is unchanged in Phase 1.** Session memory still uses inline system-prompt expansion. The `semantic_search` tool and memory are independent systems in this release.
- **No advanced retrieval.** Phase 1 uses cosine similarity only — no reranking, HyDE, or contextual retrieval.
- **Sensitive data.** reyn does not redact sensitive content before indexing. Do not index secrets, credentials, or PII unless you understand the implications. A redaction policy is planned for Phase 2.
- **Embedding requires either an API key OR local-embed extras.** OpenAI-backed classes (`light` / `standard` / `strong`) need `OPENAI_API_KEY`; local classes (`local-mini` / `local-e5`) need `pip install 'reyn[local-embed]'` and a one-time model download. See [§Embedding configuration](#embedding-configuration). A fully credential-free, zero-extras `semantic_search` path is not yet available.

## Operational Intelligence — `semantic_search` on events

The same `semantic_search` op works on Reyn's own P6 execution event log once it has been indexed into a source (conventionally named `"events"`) using the same `index_update()` pattern as any other corpus. See [Concepts: Operational Intelligence](operational-intelligence.md) for the chunk-metadata shape, example queries, and the current state of that indexing path.

## See also

- [Guide: Build a RAG corpus](../../guide/for-users/build-a-rag-corpus.md) — the *other* RAG: the builtin user-RAG pipelines over an external sqlite store (proposal 0063)
- [Reference: `reyn source`](../../reference/cli/source.md) — manage indexed sources from the CLI
- [ADR-0033](../../deep-dives/decisions/0033-rag-extensible-os.md) — design rationale and full technical spec (internal)
- [Concepts: workspace](../runtime/workspace.md) — how `.reyn/` state is structured
- [Concepts: permission model](../runtime/permission-model.md) — `index_drop` permission gate
- [Concepts: secret handling](../runtime/secret-handling.md) — embedding API key management
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — `embedding:` section schema
