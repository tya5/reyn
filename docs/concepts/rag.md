---
type: concept
topic: rag
audience: [human, agent]
---

# RAG (Retrieval-Augmented Generation)

reyn ships a RAG **framework foundation** — five primitive ops, an extensible `IndexBackend` protocol, an `EmbeddingProvider` protocol, and the stdlib `index_docs` skill — that lets you index any document corpus and have the LLM retrieve relevant chunks at query time, without ever overloading the context window with the full corpus.

**The differentiation: skill-driven indexing.** LangChain and LlamaIndex give you a Python pipeline; reyn gives you a `skill.md`. Override the chunker per-source by swapping a single python step in the postprocessor chain. The Phase 1 LLM still picks the chunking strategy, but it picks from a closed candidate set defined in your strategy skill — not from open-ended training memory.

**Phase 1 scope (= 1.0 release).** The framework foundation, the SQLite default backend (≤100K chunks, sub-second query), the LiteLLM embedding passthrough, and the stdlib `index_docs` skill ship in 1.0. Vector store plugin variety (Qdrant / FAISS / Weaviate / Pinecone), advanced retrieval (rerank / HyDE / contextual retrieval), RAG eval frameworks, and IDE integration are post-1.0 (= phase 2) territory — see [care-boundary.md](care-boundary.md). If you need that ecosystem today, LangChain / LlamaIndex are the better fit.

**TL;DR:** Index once with `reyn run index_docs`. The LLM calls the built-in `recall` tool automatically when it needs information. Override the chunking strategy per-source with a single `skill.md` file.

## Quick start

```bash
# 1. Index your docs (= the index_docs skill takes a JSON artifact as input)
reyn run index_docs '{"source": "my_docs", "path": "docs/**/*.md", "description": "Project documentation"}'

# 2. Start chatting — the LLM will recall chunks when needed
reyn chat
> Summarise the authentication design from the docs
```

Verified end-to-end with real `gemini-embedding-001` via the LiteLLM proxy: 21 EN concept docs → 418 chunks indexed (~$0.001), and natural concept queries ("What is X in Reyn?", "Explain Reyn's permission model") returned the indexed semantic answers in 3/3 chat runs (= batch 22, 2026-05-10). See `docs/deep-dives/journal/dogfood/2026-05-10-batch-22-affordance-bias-fix/findings.md`.

Behind the scenes the LLM calls `recall` and retrieves the top matching chunks:

```
LLM internally calls: recall(query="authentication design", sources=["my_docs"], top_k=5)
```

You can also index user notes or any file glob:

```bash
reyn run index_docs '{"source": "memory", "path": ".reyn/memory/*.md", "description": "User notes and session memos"}'
```

## What is a "source"

A **source** is a named collection of chunks from a set of files. You give it:

| Field | Example | Purpose |
|-------|---------|---------|
| `source` | `my_docs` | Logical name used in `recall` calls and `reyn source` commands |
| `path` | `docs/**/*.md` | Single glob pattern — all matching files are indexed together |
| `description` | `"Project documentation"` | Required. Helps the LLM decide when to search this source |

One invocation covers one source, one path, one chunking strategy. To index multiple file types with different strategies, run `index_docs` once per source and then combine them at query time using `sources=[...]`:

```
recall(query="...", sources=["python_src", "my_docs", "memory"], top_k=5)
```

Source metadata is persisted in `.reyn/index/sources.yaml`. Once indexed, a source appears automatically in the LLM's context on every chat turn:

```
## Indexed sources (3 available)

- **memory** — User notes / past session memos (142 chunks)
- **reyn_code** — Reyn Python framework code (1247 chunks)
- **my_docs** — Project documentation (89 chunks)

Use the `recall` tool with `sources=[<name>, ...]` to search.
```

## The `recall` tool

`recall` is a built-in tool available to the LLM in every chat session. It takes a natural-language query, searches the requested sources, and returns the top-K matching chunks:

```
recall(query="plan-mode discussion", sources=["memory"], top_k=5)
```

The LLM picks which sources to search based on the source descriptions you provided at index time. You do not need to configure which sources a skill may use — any indexed source is accessible.

Internally, `recall` embeds the query using the same model used for indexing, runs a cosine-similarity search against each source's SQLite index, and merges results ranked by similarity score. The entire operation is deterministic; the LLM sees only the top-K chunks as text, never the raw vectors.

A second built-in tool, `drop_source`, lets the LLM drop an index on your behalf — useful when iterating on a chunking strategy:

```
drop_source(source="my_docs")
```

## Indexing strategy

When you run `index_docs`, the LLM examines a sample of your files and decides on a chunking strategy. Three built-in chunkers are available:

| Chunker | Best for |
|---------|---------|
| `heading` | Markdown / RST — splits on heading boundaries |
| `blank_line` | Plain prose — splits on paragraph breaks |
| `sentence` | Dense text — splits sentence-by-sentence |

The LLM's strategy decision is constrained by the same P4 mechanism used for all phase transitions: it picks from the declared chunker options, cannot invent new ones, and the choice is validated against the schema before the postprocessor runs.

The chunking step runs deterministically in `Skill.postprocessor` — no LLM involvement, no attractor surface. The LLM's one decision (strategy selection) takes place in Phase 1; all subsequent steps (split → embed → write) are pure computation.

## Override the chunker

The default chunker covers common cases. For specialised corpora — Python source code, SQL schemas, structured YAML — you can replace the chunking logic entirely with a custom Python module and a minimal `skill.md` overlay:

```yaml
# reyn/project/index_python_src/skill.md
extends: stdlib/index_docs

phases:
  strategy:
    instructions_override: |
      Python AST chunking — split on function and class boundaries.
      Each chunk includes the full function or class body.

postprocessor:
  steps:
    - type: python
      module: reyn.project.index_python_src.ast_chunkers
      function: apply_strategy
```

Your `ast_chunkers.py` module receives the strategy artifact and the file path glob, and returns a list of chunks. The rest of the pipeline (embed → index_write) is unchanged.

This is the core skill-DSL differentiator: you describe your chunking logic in natural language and Python; the OS handles embedding and indexing. See the skill author guide for a full walkthrough.

## Storage location

All index data is stored inside your project's `.reyn/` directory:

```
.reyn/
  index/
    sources.yaml                   # Source manifest — name, path, model, chunk count
    my_docs/
      index.db                     # SQLite vector store for this source
    memory/
      index.db
```

`sources.yaml` is the single source of truth for what is indexed. The SQLite files contain the chunk text and embedding vectors. You can inspect them with any SQLite client, though the schema is internal.

Phase 1 uses SQLite as the only storage backend. Phase 2 will add pluggable backends (Qdrant, FAISS, Pinecone) via a `register_backend()` extension point.

## Permissions

Two permission gates protect RAG operations:

| Permission | Default | Trigger |
|-----------|---------|---------|
| `permissions.embed` | `ask` | First embedding call per skill run |
| `permissions.index_drop` | `ask` | `drop_source` tool call or `reyn source rm` |

`permissions.embed: ask` means the first time `index_docs` tries to call the embedding API, reyn prompts you to approve. You can pre-approve in `reyn.yaml`:

```yaml
permissions:
  embed: allow
```

The stdlib `index_docs` skill ships with `embed: allow` in its own permissions block, so the prompt only fires if you are running a custom override that hasn't inherited this setting.

## Cost

Embedding costs are linear in chunk count. A single `index_docs` run for a typical documentation set costs around **$0.0003** for the strategy-selection LLM call (one call per invocation, using the default model). Embedding cost depends on your corpus size and embedding model — `text-embedding-3-small` is the default.

reyn protects against unexpected large bills with a cost preflight gate:

- Before embedding begins, reyn estimates the chunk count from the file glob.
- If the estimate exceeds `cost_warn_threshold` (default: 10,000 chunks), reyn prompts you for confirmation before starting.
- You can adjust the threshold in `reyn.yaml`:

```yaml
embedding:
  cost_warn_threshold: 5000    # ask before indexing more than 5K chunks
```

Progress feedback is emitted during long indexing runs:

```
Embedded 5K / 100K chunks (5%), ETA 25 min
```

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

For chat-side action retrieval specifically (= `search_actions`), see [Guide: enable semantic search](../guide/for-users/enable-semantic-search.md) and the [`reyn embeddings`](../reference/cli/embeddings.md) CLI for cache management.

## Phase 1 scope

**Included in Phase 1 (1.0 release):**

- `index_docs` stdlib skill with heading / blank_line / sentence chunkers
- `recall` tool available to the LLM in every chat session
- `drop_source` tool for cleanup
- SQLite vector store backend
- `reyn source list / describe / rm` CLI
- Cost preflight gate and progress feedback
- Override pattern (`extends: stdlib/index_docs` + custom Python module)
- Empty-state hint in the chat system prompt

**Deferred to Phase 1.5 (1.1+):**

- Memory layer migration from inline expansion to `recall(sources=["memory"])`. Memory continues to work as-is in 1.0.

**Landed post-1.0:**

- Local embedding models via sentence-transformers (FP-0043, 2026-05) — see [§Local embedding backend](#local-embedding-backend-fp-0043). The chat-side `search_actions` surface is the first consumer; the same `local-mini` / `local-e5` classes are reachable from `embedding.default_class` for document indexing too.

**Deferred to Phase 2 (post-1.1):**

- Alternative vector store backends (Qdrant, FAISS, Pinecone)
- Incremental re-indexing on file change
- Advanced retrieval (rerank, HyDE, contextual retrieval)
- Additional local backends (ollama, ONNX, GGUF)
- RAG evaluation framework

## Limitations

- **100K chunks recommended maximum** per source for Phase 1 SQLite backend. Larger corpora will work but query latency increases.
- **No incremental indexing.** Re-running `index_docs` with `mode: replace` (the default) re-indexes the full source. Use `mode: append` only when you know the new files do not overlap with existing chunks.
- **Memory layer is unchanged in Phase 1.** Session memory still uses inline system-prompt expansion. The `recall` tool and memory are independent systems in this release.
- **No advanced retrieval.** Phase 1 uses cosine similarity only — no reranking, HyDE, or contextual retrieval.
- **Sensitive data.** reyn does not redact sensitive content before indexing. Do not index secrets, credentials, or PII unless you understand the implications. A redaction policy is planned for Phase 2.
- **Embedding requires either an API key OR local-embed extras.** OpenAI-backed classes (`light` / `standard` / `strong`) need `OPENAI_API_KEY`; local classes (`local-mini` / `local-e5`) need `pip install 'reyn[local-embed]'` and a one-time model download. See [§Embedding configuration](#embedding-configuration). A fully credential-free, zero-extras `recall` path is not yet available.

## Operational Intelligence — `recall` on events

The `index_events` stdlib skill (FP-0009 Component A) populates a source named
`"events"` by chunking the P6 event log (`.reyn/events/*.jsonl`) on run
boundaries: one chunk per skill execution. This makes Reyn's own execution
history semantically searchable through the standard `recall` op — no new
op kinds required.

### Source name

```
sources: ["events"]
```

`index_events` always writes to this fixed source name. Run it once (or
schedule it periodically) to keep the index current:

```bash
reyn run index_events '{"period": "last-7d"}'
```

### Chunk metadata

Each chunk carries structured metadata in `extra`:

| Field | Type | Example |
|-------|------|---------|
| `skill` | string | `"swe_bench"` |
| `skill_version_hash` | string | `"abc123..."` |
| `started_at` / `completed_at` | ISO datetime | `"2026-05-10T09:15:00Z"` |
| `duration_seconds` | number | `43` |
| `status` | `"success"` \| `"failed"` \| `"aborted"` | `"failed"` |
| `phases` | list[string] | `["explore","plan","verify"]` |
| `errors` | list | `[{"phase": "verify", "msg": "..."}]` |
| `tool_calls` | object | `{"grep": 3, "shell": 1}` |
| `cost_usd` | number | `0.18` |

The chunk text is a human-readable run summary; `extra` fields are attached as
metadata but are not directly filterable — the LLM cannot issue structured
`WHERE status="failed"` queries. The recommended pattern is: issue a semantic
query to surface relevant chunks, then filter in post-processing logic.

### Typical queries

**Failure patterns for a specific skill:**

```yaml
- type: run_op
  op:
    kind: recall
    query: "my_skill failure error phase"
    sources: ["events"]
    top_k: 20
  output_name: trace_summary
```

This returns chunks where `my_skill` appears prominently in the run text,
biased toward runs that mention errors and failures. Combine with a
post-filter on `chunk.metadata.extra.status == "failed"` for precise results.

**Surfacing error excerpts:**

```
query: "PermissionError が起きた run"
sources: ["events"]
top_k: 10
```

Because error messages are embedded in the chunk text itself, semantic
similarity surfaces runs where that error class appeared — even without
structured filtering.

**Top-cost skills (recent period):**

```
query: "高コスト high cost expensive run"
sources: ["events"]
top_k: 20
```

The LLM cannot directly sort by `cost_usd` (no numeric range query in Phase 1
SQLite backend). Return top-K semantically relevant chunks and sort in Python
using `chunk.metadata.extra["cost_usd"]`.

### Example skill usage

A skill phase that collects execution traces before analysis:

```yaml
- type: run_op
  op:
    kind: recall
    query: "{{ input.skill_name }} failure error phase"
    sources: ["events"]
    top_k: 20
  output_name: trace_summary
```

The `trace_summary` artifact contains `trace_summary.chunks` — a list of the
top-K matching run summaries. Downstream phases read this list directly.

### Empty-index fallback

If `index_events` has never been run, `sources=["events"]` returns an empty
result (`trace_summary.chunks` has length 0). A skill should detect this and
either:

1. Emit a `run_skill` op to invoke `index_events` first, then retry `recall`.
2. Fall back to direct file reads:
   ```yaml
   - type: run_op
     op:
       kind: file
       op: glob
       path: ".reyn/events/*.jsonl"
     output_name: event_files
   ```

The `ops_report` stdlib skill (FP-0009 Component D) implements option 1
as its `collect` phase.

### Cross-references

| Consumer | Uses events source for |
|----------|----------------------|
| FP-0006 `collect_traces` | failure pattern retrieval for skill self-improvement |
| FP-0007 evaluation reports | regression detection across eval runs |
| FP-0008 SWE-bench | past-case retrieval for analogous repository fixes |
| `ops_report` stdlib skill | weekly/periodic operational summary generation |

See [FP-0006](../deep-dives/proposals/0006-skill-self-improvement.md),
[FP-0007](../deep-dives/proposals/0007-evaluation-infrastructure.md),
[FP-0008](../deep-dives/proposals/0008-swe-bench-integration.md) for consumer
design details.

## See also

- [Reference: `reyn source`](../reference/cli/source.md) — manage indexed sources from the CLI
- [ADR-0033](../deep-dives/decisions/0033-rag-extensible-os.md) — design rationale and full technical spec (internal)
- [Concepts: workspace](workspace.md) — how `.reyn/` state is structured
- [Concepts: permission model](permission-model.md) — `embed` and `index_drop` permission gates
- [Concepts: secret handling](secret-handling.md) — embedding API key management
- [Reference: `reyn.yaml`](../reference/config/reyn-yaml.md) — `embedding:` section schema
