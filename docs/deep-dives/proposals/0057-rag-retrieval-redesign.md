# FP-0057 — RAG / Retrieval redesign (proposal, draft)

**[lead-coder]** — Owner-designed in consultation; captured for architect co-vet + phasing. **Design VALIDATED by owner; implementation dispatch awaits owner GO (multi-PR spend).**

## Motivation / current state

Retrieval is a charter "thin area." Current state (primary-mapped):
- The in-core substrate is solid: pluggable `IndexBackend` + `EmbeddingProvider` registries (`register_backend`/`register_provider`), per-source SQLite index, `recall` tool, cosine top-k.
- **Ingestion is CodeAct-only**: `embed_and_index` (`reyn.api.safe.embed_index`) is the ONLY entry — no CLI, no op. `#1303` folded the old `embed`+`index_write` run-ops for provider-direct/skill-decouple; that reason is **obsolete** (skill engine deleted `#2438`), so re-exposing typed ops is clean.
- **Auto-retrieval / preprocessor was deleted** (`d3c8c7a1`); retrieval is 100% agent-invoked. (CLAUDE.md's charter quote still names "the preprocessor step" — **stale**, reconcile.)
- No reranking / hybrid / ANN (cosine only, ~100K chunk ceiling).

## Scope & triggers (near-term)

- **Trigger = LLM (tool call) or slash command / CLI.** `embed` / `index_update` / `semantic_search` fire explicitly. **NO automatic index/search in near-term.**
- **Deferred to a future automation arc** (explicitly out of near-term scope): automatic retrieval (preprocessor injection); automatic (file-change) background reindex; the **ephemeral-attachment auto-flow** (attach → auto-vectorize → auto-retrieve). The attachment *parts* (embed, ephemeral backend, size-gate) may be built near-term but fire via LLM/slash manually; the attach-time automation lands later. The ephemeral-RAG industry survey is retained as the design basis for that future work.
- **No technical debt (clean-break):** where the redesign replaces a path, remove the old one — do not run both. Concretely: CodeAct-only ingestion → replaced by typed tools (old entry retired); events DIY-CodeAct indexing → replaced by a bundled internal events source. The deleted preprocessor stays deleted until the future automation arc rebuilds it cleanly — no half-built seam now. Parts are designed so future automation layers on without debt.

## Core frame: **shared embed + pluggable vessel**, split by **audience surface**

Two axes:
- **SHARED — `embed` (EmbeddingProvider)**: reyn is the **sole embedder**, one config → model consistency across every use-case (vectors from reyn's embed land in whatever vessel; no cross-boundary model lockstep problem).
- **PLUGGABLE — vessel = `IndexBackend`** (already a registry): chosen per source/use-case.

### Two surfaces (by audience)

| Surface | For | Tools the LLM/caller sees | Internals |
|---|---|---|---|
| **reyn-internal** | tool-use / memory / events (+ ephemeral attachments) | **`index_update` + `semantic_search`** (high-level, encapsulated) | reyn uses embed-equivalent code + in-core store *inside* the tool. **embed step NOT exposed. NO pipeline.** |
| **user-facing** | user's intentional persistent RAG | **`embed`** (raw primitive) | user composes `embed` → their own MCP vessel **via pipeline** |

The embedding **logic** is shared (same `EmbeddingProvider` behind the user `embed` tool and inside `index_update`/`semantic_search`) — different surfaces, one logic. `embed` is a **user-facing** tool; reyn merely also uses the same logic internally.

### Vessel backends (pluggable `IndexBackend`)

- **in-core SQLite** → reyn-internal (tool-use = `action_retrieval` exists; memory = make semantic, old Phase 1.5; events = operational-intelligence recall). store/retrieve closed in-core.
- **NEW MCP-backed adapter** → user intentional RAG. `write`/`query` delegate to the user's MCP vector-DB. reyn provides embed + calls MCP; **does not manage the vessel**.
- **ephemeral/transient** → attachment RAG. Throwaway + TTL teardown, classified **cache, NOT recovery-core** (must not become a WAL-derived recovery source). **Size-gate**: small attachment → full-context (Anthropic long-context philosophy: no RAG below ~110k–200k tokens); large → ephemeral index. (Industry: "ephemeral" = persistent-but-expiring TTL; vectorize at attach time.)

`recall` / ingestion are **backend-agnostic** (operate over whatever backend the source is configured with).

**Backend capability caveat (incremental × MCP):** `index_update`'s delta-reconcile relies on the backend's `existing_hashes`. SQLite supports it; an **MCP vector-DB backend may not expose "which content_hashes exist"** → incremental can't run there. The MCP adapter must declare capability: if `existing_hashes` is unsupported, either delegate to the MCP server's own upsert semantics or fall back to full-replace — never silently no-op. `IndexBackend` should carry a capability flag.

### 🔑 Consolidate the two parallel indexes (the headline no-tech-debt refactor)

Today `SqliteIndexBackend` (doc RAG, `.reyn/cache/index/<source>/`) and `ActionEmbeddingIndex` (tool-use RAG, `.reyn/cache/action_index/`) are **structurally parallel but separately implemented** — duplicated cosine math (numpy vs hand-rolled), duplicated PID advisory-lock (two shapes), duplicated catalog/content-hash dedup. **Fold `ActionEmbeddingIndex` into the pluggable `IndexBackend`**: tool-use becomes "just another source on a backend" (its catalog = a source, its chunker = 1-action-per-chunk). Kills the duplication; `search_actions` keeps its surface but rides the unified store. This is the concrete realization of "shared embed + pluggable vessel" and the primary clean-up.

### Chunking (pluggable `Chunker` registry — an extensibility **hedge**; the shipped defaults do the real work, no heavy near-term investment)

- **reyn-internal = per-domain chunkers, usually NATURAL record units** (reyn knows its own data shape): **tool-use = 1 action = 1 chunk over the FULL invokable catalog — primitive tools + skills + MCP tools + pipelines** (not primitive tools only; verify current `action_retrieval`/`search_actions` coverage and extend to all four kinds); memory = 1 file / section; **events = SIMPLIFIED for now** (e.g. 1 line = 1 event; the run-grouping / operational-intelligence "run-chunk" strategy is a **future discussion**, not designed now). Not token-window slicing.
- **ephemeral attachments (arbitrary / unknown structure)**: the **size-gate carries the load** — small attachments → full-context, **no chunking**; only genuinely large files chunk. For those, a **generic structure-agnostic default chunker** (recursive / token ~800–1024 + overlap) suffices (throwaway / used-once → low chunk-quality requirement). Optional light format-aware chunking (markdown / code / PDF) as a later enhancement, **not required**.

### Default embed = local MiniLM (HF download) — offline/restricted-network is a Reliability concern

The **default** embedding class is `local-mini` = `sentence-transformers/all-MiniLM-L6-v2` (`config/embedding.py:42`, default since FP-0043 Phase 4), used by default for tool-use RAG (`action_retrieval`/`search_actions`). It **downloads ~22MB from HuggingFace lazily on first use**; the model cache lives under the reyn cache root. In a **corporate/firewalled network where HF is blocked**, the load fails and emits `"failed to load … Check network connectivity …"`, and the index build degrades (owner hit this at their company). Escape hatch exists (`action_retrieval.embedding_class: standard` → API embedding), but the DEFAULT triggers the HF download.

**Redesign must make the offline/air-gapped story clean** (Reliability + Product-Think): bundle the model / `HF_HUB_OFFLINE`+`local_files_only` support + a clear message / a clean degrade. Cross-cutting: affects every RAG use-case on the shared embed layer that uses `local-mini`.

### Existing retrieve mechanisms — integrate, don't reinvent

Each internal domain ALREADY has some retrieve support; the redesign must integrate/replace deliberately (semantic RAG **complements** existing exact/structured access — different query patterns, both kept):

| domain | existing | kind | integrate/replace |
|---|---|---|---|
| tool-use | `search_actions` (`retrieval` scheme + action embedding index, `reyn embeddings` CLI) | **semantic** (already RAG) | **INTEGRATE/EXTEND** — reuse; extend catalog to 4 action kinds |
| memory | `reyn memory search` CLI + `find_one(query, entries)` + inline expansion + markdown TOC | keyword/exact | **DECIDED**: keep exact access (list/read/name + `reyn memory search`) AND add semantic recall as a memory source — complementary, no supersede. Resolve the standing inline-TOC-vs-semantic **permanent fork** (deferred Phase 1.5 that never landed) by making semantic a first-class source; inline TOC stays for always-on injection. (`recall_docs` planned-but-unbuilt → subsumed by this memory-as-source, or dropped.) |
| events | `reyn events --filter/--since/--agent/--conversation` (structured filter) + DIY semantic `recall(sources=["events"])` (CodeAct-only, no bundled indexer; `operational-intelligence.md`) | structured filter + half-semantic (DIY) | keep structured filter; **REPLACE** DIY-CodeAct indexing with a bundled/turnkey internal events source |

Principle: exact/structured access (memory search, events filter) stays; the redesign unifies the **semantic** path under the source/backend model and **reuses `search_actions`** where retrieval is already semantic.

### Source = the unit; source-bound embedding model; source-parameterized ops

- **`index_update` (single source) / `semantic_search` (one or more sources)** are **source-parameterized** — confirmed existing (`embed_and_index(source=…)`, `recall(sources=[…])`). Kept.
- **Each source is bound to ONE embedding model** — already recorded (`SourceManifest.embedding_model` + index `meta.embedding_model`; `embedding_model` is also a per-chunk column + query filter). So models are separated at **source granularity** (different model → different source).
- **Correctness hardening (redesign):** `semantic_search(source)` must **auto-adopt the source's recorded embedding model** (read from the manifest) rather than a caller/config-supplied model — today `recall` embeds the query with `op.embedding_model`, with no strong guard that it matches the source's indexed model (a mismatch → meaningless cosine, silent bug). Enforce per-source model consistency.

### `index_update` = incremental / delta-reconcile ONLY

`index_update` reconciles the index against the current content — **no full-rebuild mode is exposed**:
- **add** new chunks (content_hash in source, not in index) → embed + insert
- **update** changed chunks (content_hash differs) → re-embed + replace
- **remove** deleted chunks (in index, gone from source) → delete
- **skip** unchanged (same content_hash) → no-op (reuses existing `existing_hashes` pre-embed dedup)

Extends the current `existing_hashes` add-only/skip behavior with **deletion + modification detection** (today only full `mode="replace"` reflects deletions/changes — that gap is closed). A from-scratch rebuild = `index_drop` → `index_update` on an empty index (all "new"). Re-embed cost = the delta only (Merkle-style change detection philosophy).

## Tool surface & naming

New surface (resolves the observed `recall`↔`search_actions`↔`memory` confusion):

| new | replaces / relates to | note |
|---|---|---|
| `embed` (user-facing) | (new — exposes the embed primitive) | batch: list→vectors |
| `index_update` (internal) | `embed_and_index` (CodeAct-only entry retired) | incremental only; source-parameterized |
| `semantic_search` (internal) | `recall` (renamed to break the naming collision) | source(s)-parameterized; auto-adopts source model |
| `search_actions` | kept (tool-use RAG surface) | now rides the unified `IndexBackend` |
| `index_drop` / `drop_source` | kept | gated |

The internal `index_update`/`semantic_search` **call the same `embed` primitive** (no duplicated embed logic) — encapsulation ≠ re-implementation.

## Permissions & cross-cutting concerns

- **Permissions**: `embed` / `index_update` / `semantic_search` = **default ALLOW** (compute / read / own-index-write; gating = friction), individually name-gateable via `contextual_gate`. `index_drop` (destructive) stays **gated (ask)**.
- **Cost/budget (band)**: embed has real cost (API $ or compute) yet is default-allow and `cost_estimator` is currently dead-code from the ingestion caller. **Wire `cost_estimator` + `cost_warn_threshold` into `index_update`** so large ingestions surface/warn cost even without a permission prompt (do not leave cost unbounded + the estimator dead).
- **Security (redaction)**: ingestion has **no PII/secret scan** today (doc just warns operators). Esp. for **ephemeral attachments** (user attaches a secret-bearing file → embedded). Consider a redaction/scan hook on the ingestion path (at least flag; align with the Memory-write threat-scan precedent).
- **Offline/air-gapped**: see the local-MiniLM section — default embed downloads from HF; make the degrade clean.
- **Recovery classification**: ephemeral store = cache, written OUTSIDE the `.reyn/` recovery-core write-gate (not a WAL-derived recovery source); explicit teardown.

## Composition

- **Pipeline is user-facing ONLY** — for user RAG (`embed` op → their MCP store op). In-core does **not** use pipeline; it uses the encapsulated `index_update`/`semantic_search` tools. No standalone composable `index_write` op is needed for in-core (the tool encapsulates the store write); the user vessel's store is MCP-side.

## Phasing (implementation — awaits GO)

0. **Foundation — consolidate the two indexes.** Fold `ActionEmbeddingIndex` into the pluggable `IndexBackend` (unify cosine + advisory-lock + dedup); add an `IndexBackend` capability flag (`existing_hashes` support). Everything else rides this unified store, so it goes first.
1. **`embed` typed op/tool** (user-facing; batch list→vectors, `batch_size=100`; preserve `existing_hashes`). Retires the CodeAct-only entry.
2. **`index_update` (incremental/delta, source-bound model, cost-estimator wired) + `semantic_search` (renamed from `recall`, auto-adopts source model)** encapsulated tools over the in-core backend. Wire tool-use (via the consolidated action source) / memory-semantic / events onto them.
3. **MCP-backed `IndexBackend` adapter** (user vessel; thin delegate on `register_backend`; declares `existing_hashes` capability + incremental fallback).
4. **Ephemeral attachment RAG**: transient backend + TTL teardown (recovery-core-excluded) + **size-gate** + a **redaction hook** consideration.
5. Cross-cutting: **offline/air-gapped** degrade for the default local-MiniLM.
6. Later / separate arc: **auto-insert seam** (reyn embeds query + calls configured retrieve + injects — owns the seam, not the store); **builtin ingestion pipeline** (task #66); advanced (rerank/hybrid/ANN).

## Follow-on / open

- **Builtin mechanism** (task #66): reyn ships builtin mcp/skill/pipeline; the RAG ingestion turnkey = a **builtin pipeline**. After RAG parts land.
- **Doc reconciliation**: CLAUDE.md charter "preprocessor step" quote is stale — fix.
- Advanced (rerank / hybrid / ANN) deferred; cosine sufficient near-term.
- Clean-break, no compat shim (owner).
