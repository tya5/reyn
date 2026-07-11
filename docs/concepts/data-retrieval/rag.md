---
type: concept
topic: rag
audience: [human, agent]
---

# RAG (Retrieval-Augmented Generation)

reyn ships a RAG **framework foundation** — five primitive ops, an extensible `IndexBackend` protocol, an `EmbeddingProvider` protocol, and a safe-mode `embed_and_index()` entry point — that lets you index any document corpus and have the LLM retrieve relevant chunks at query time, without ever overloading the context window with the full corpus.

**The differentiation: retrieval is a built-in tool, not a library call.** LangChain and LlamaIndex give you a Python pipeline you call from your own driver code. reyn's `semantic_search` and `drop_source` are built-in tools the LLM itself calls during a normal `reyn chat` session — no orchestration code required on the search side.

**Phase 1 scope (= 1.0 release).** The framework foundation, the SQLite default backend (≤100K chunks, sub-second query), and the LiteLLM embedding passthrough ship in 1.0. Vector store plugin variety (Qdrant / FAISS / Weaviate / Pinecone), advanced retrieval (rerank / HyDE / contextual retrieval), RAG eval frameworks, and IDE integration are post-1.0 (= phase 2) territory — see [../architecture/care-boundary.md](../architecture/care-boundary.md). If you need that ecosystem today, LangChain / LlamaIndex are the better fit.

**TL;DR:** Search is automatic — the LLM calls the built-in `semantic_search` tool whenever it needs information from an indexed source. Creating a source requires a short safe-mode Python step that reads your files and calls `embed_and_index()` (there is no bundled one-command indexing skill).

## Quick start

Indexing a corpus is a small script run once as a safe-mode `python` step — read the files, split them into chunks, hand them to `embed_and_index`:

```python
# my_project/index_docs.py — run once via a `python` step (mode: safe by default)
from reyn.api.safe import file, embed_index

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

embed_index.embed_and_index(
    chunks,
    source="my_docs",
    model="text-embedding-3-small",
    mode="replace",
    description="Project documentation",
    path="docs/**/*.md",
)
```

```bash
# Start chatting — the LLM will recall chunks when needed
reyn chat
> Summarise the authentication design from the docs
```

Verified end-to-end with real `gemini-embedding-001` via the LiteLLM proxy: 21 EN concept docs → 418 chunks indexed (~$0.001), and natural concept queries ("What is X in Reyn?", "Explain Reyn's permission model") returned the indexed semantic answers in 3/3 chat runs (= batch 22, 2026-05-10). See `docs/deep-dives/journal/dogfood/2026-05-10-batch-22-affordance-bias-fix/findings.md`. (That run predates the `embed_and_index()` entry point and used the since-removed `index_docs` skill — the underlying embed/index/search mechanics are unchanged; `recall` was renamed `semantic_search` in FP-0057 Phase 2a.)

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

Source metadata is persisted in `.reyn/index/sources.yaml`. Once indexed, a source appears automatically in the LLM's context on every chat turn:

```
## Indexed sources (3 available)

- **memory** — User notes / past session memos (142 chunks)
- **reyn_code** — Reyn Python framework code (1247 chunks)
- **my_docs** — Project documentation (89 chunks)

Use the `semantic_search` tool with `sources=[<name>, ...]` to search.
```

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

There is no bundled chunker and no LLM-driven strategy selection — the chunking logic in the [Quick start](#quick-start) example (paragraph split) is plain Python you write and adapt per corpus. For specialised corpora — Python source code, SQL schemas, structured YAML — swap in whatever splitting logic fits (e.g. an AST-based splitter for source code, a heading-based splitter for Markdown) before calling `embed_and_index`.

The chunking step runs deterministically in your `python` step — no LLM involvement, no attractor surface. `embed_and_index` handles embedding and index writes; everything upstream of that call (reading files, splitting into chunks) is ordinary Python.

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

There is no dedicated permission gate on `embed_and_index()` itself — a safe-mode `python` step that calls it runs under the calling phase's ordinary python-step permissions, not a RAG-specific one.

## Cost

Embedding cost is linear in chunk count and depends on your corpus size and embedding model — `text-embedding-3-small` is the default. There is no built-in cost preflight or progress reporting for a hand-written indexing step (unlike the removed `index_docs` skill's wrapper) — estimate chunk count from your own file glob before running a large indexing job if cost matters.

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
  cost_warn_threshold: 10000
```

Dispatch is **provider-prefix-based**: classes whose `model` string starts with `sentence-transformers/` route to the local backend; everything else (`openai/`, future LiteLLM-routable providers) routes through LiteLLM. Existing OpenAI-backed callers are byte-identical to pre-FP-0043; the routing wrapper passes them through transparently.

The OpenAI API key is read from `~/.reyn/secrets.env` via `${OPENAI_API_KEY}` — no literal value in `reyn.yaml`. After setting the key with `reyn secret set OPENAI_API_KEY`, indexing with `standard` / `light` / `strong` works out of the box with no further configuration.

### Local embedding backend (FP-0043)

`local-mini` and `local-e5` use [sentence-transformers](https://www.sbert.net/) to embed locally (= no API, no credentials, no per-query cost). They are gated behind an `extras` install so the base `reyn` package stays small:

```bash
pip install 'reyn[local-embed]'
```

This pulls `sentence-transformers >= 2.7` and `torch >= 2.0`. The model itself downloads on first use (~22 MB for `local-mini`, ~118 MB for `local-e5`) and caches under `~/.cache/reyn/sentence-transformers/` (overridable via `REYN_CACHE_DIR` / `XDG_CACHE_HOME`).

Device selection is `cpu` by default; opt into GPU acceleration via the `REYN_EMBED_DEVICE` env var (`mps` for Apple Silicon, `cuda` for NVIDIA). Invalid values warn and fall back to `cpu`.

For chat-side action retrieval specifically (= `search_actions`), see [Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md) and the [`reyn embeddings`](../../reference/cli/embeddings.md) CLI for cache management.

## Phase 1 scope

**Included in Phase 1 (1.0 release):**

- `embed_and_index()` safe-mode entry point for indexing (`reyn.api.safe.embed_index`)
- `semantic_search` tool available to the LLM in every chat session
- `drop_source` tool for cleanup
- SQLite vector store backend
- `reyn source list / describe / rm` CLI
- Empty-state hint in the chat system prompt

**Deferred to Phase 1.5 (1.1+):**

- Memory layer migration from inline expansion to `semantic_search(sources=["memory"])`. Memory continues to work as-is in 1.0.

**Landed post-1.0:**

- Local embedding models via sentence-transformers (FP-0043) — see [§Local embedding backend](#local-embedding-backend-fp-0043). The chat-side `search_actions` surface is the first consumer; the same `local-mini` / `local-e5` classes are reachable from `embedding.default_class` for document indexing too.

**Deferred to Phase 2 (post-1.1):**

- Alternative vector store backends (Qdrant, FAISS, Pinecone)
- Incremental re-indexing on file change
- Advanced retrieval (rerank, HyDE, contextual retrieval)
- Additional local backends (ollama, ONNX, GGUF)
- RAG evaluation framework

## Limitations

- **100K chunks recommended maximum** per source for Phase 1 SQLite backend. Larger corpora will work but query latency increases.
- **No incremental indexing.** `embed_and_index`'s `mode="append"` default skips chunks whose `content_hash` is already indexed but does not detect deleted/changed source files; pass `mode="replace"` to rebuild a source from scratch when files change.
- **Memory layer is unchanged in Phase 1.** Session memory still uses inline system-prompt expansion. The `semantic_search` tool and memory are independent systems in this release.
- **No advanced retrieval.** Phase 1 uses cosine similarity only — no reranking, HyDE, or contextual retrieval.
- **Sensitive data.** reyn does not redact sensitive content before indexing. Do not index secrets, credentials, or PII unless you understand the implications. A redaction policy is planned for Phase 2.
- **Embedding requires either an API key OR local-embed extras.** OpenAI-backed classes (`light` / `standard` / `strong`) need `OPENAI_API_KEY`; local classes (`local-mini` / `local-e5`) need `pip install 'reyn[local-embed]'` and a one-time model download. See [§Embedding configuration](#embedding-configuration). A fully credential-free, zero-extras `semantic_search` path is not yet available.

## Operational Intelligence — `semantic_search` on events

The same `semantic_search` op works on Reyn's own P6 execution event log once it has been indexed into a source (conventionally named `"events"`) using the same `embed_and_index()` pattern as any other corpus. See [Concepts: Operational Intelligence](operational-intelligence.md) for the chunk-metadata shape, example queries, and the current state of that indexing path.

## See also

- [Reference: `reyn source`](../../reference/cli/source.md) — manage indexed sources from the CLI
- [ADR-0033](../../deep-dives/decisions/0033-rag-extensible-os.md) — design rationale and full technical spec (internal)
- [Concepts: workspace](../runtime/workspace.md) — how `.reyn/` state is structured
- [Concepts: permission model](../runtime/permission-model.md) — `index_drop` permission gate
- [Concepts: secret handling](../runtime/secret-handling.md) — embedding API key management
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — `embedding:` section schema
