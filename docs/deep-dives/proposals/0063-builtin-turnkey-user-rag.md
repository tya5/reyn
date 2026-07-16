# FP-0063 — Builtin turnkey user RAG (builtin mcp + skill + pipeline)

**[lead-coder]** — Owner-designed in consultation (2026-07-15); captured for architect co-vet + phasing. This is the arc FP-0057 named and deferred as **"task #66 (builtin mcp)"**. **Design steered by owner; implementation dispatch awaits owner GO.**

## Motivation / current state

FP-0057 split RAG by audience and shipped the in-core half. The **user-facing** half is a raw primitive: reyn exposes `embed`, and the user is expected to compose `embed` → their own MCP vector-DB via pipeline. That composition works, but **a user with a folder of documents has to assemble everything themselves** — parser, chunker, store, and both pipelines. Nothing is turnkey.

FP-0057 explicitly carved this out rather than solving it:

> **§Stores — by audience (line 38)**: A **batteries-included builtin MCP vector-DB server** (for users without their own DB) is a **separate arc — task #66 (builtin mcp)**, NOT part of FP-0057; if built it is used exactly like any external MCP vessel (MCP tools), still not in-core.
>
> **§Phasing (line 126)**: **Builtin mechanism (task #66)**: reyn ships **builtin mcp/skill/pipeline**; the RAG ingestion turnkey = a **builtin pipeline**. **After RAG parts land.**

`embed` has landed, so the stated precondition is met. FP-0057 Phase 3 (#2855, ephemeral attachment) is **in-core and independent** — it does not block this arc.

## Owner specification (2026-07-15)

- The user names **a document file or folder** + **an output sqlite filename**; reyn chunks → embeds → writes to that sqlite **with metadata**. A **query pipeline** returns **top-k**.
- Target formats: **txt / md / pdf / Excel / PowerPoint / Word**.
- **The builtin content IS the deliverable; reyn-side code change is minimal (target: zero).**
- **"MCP" means bundling standard MCP servers** — *not* turning reyn into an MCP server.
- **Policy: reuse OSS wherever possible.**
- **No reyn-side backend pluggability.** A user who wants a different vector DB **copies the builtin and re-points the MCP server**. The builtin is readable via `reyn_repo` (FP-0061), so it functions as the reference implementation. This is the extension mechanism.

## Inherited constraints (FP-0057, ratified — non-negotiable)

| # | Constraint | Source |
|---|---|---|
| C1 | **`embed` — reyn is the SOLE embedder**, one config → model consistency across every use-case | 0057 line 22 |
| C2 | **User RAG store = EXTERNAL, never in-core.** `IndexBackend` is IN-CORE ONLY. **reyn builds NO adapter / NO MCP `IndexBackend`** — no reyn-side store code for user RAG | 0057 lines 23, 38 |
| C3 | **Pipeline is user-facing ONLY** (in-core uses the encapsulated tools, never a pipeline) | 0057 line 111 |
| C4 | **One source = one embedding model** (`embedding_model` is a per-chunk column + query filter; different model → different source) | 0057 line 74 |
| C5 | **Incremental by `content_hash`**: add (in source, not in index) / update (hash differs → re-embed + replace) / remove (in index, gone from source) | 0057 lines 80–82 |

C1 and C2 together are the load-bearing pair: **reyn contributes exactly one thing (`embed`) and hosts nothing.**

## Architecture — every non-core concern rides MCP

```
builtin ingest pipeline:            input = <path> (file|folder) + <output sqlite>
  ForEach file under <path>:
    MCP   markitdown.convert_to_markdown(file:…)   → Markdown
    MCP   chunker.chunk(text, size, overlap)       → chunks
    tool  embed(texts=chunks)                      → vectors        ← the ONLY reyn-side piece (C1)
    MCP   vectordb.store(vectors, metadata, db=…)  → <user>.sqlite  ← path is the server's concern (C2)

builtin query pipeline:             input = <query> + <sqlite> + <top_k>
    tool  embed(texts=[query])                     → vector
    MCP   vectordb.query(vector, top_k, filters)   → top-k

builtin skill:  when/how to run the two pipelines
builtin mcp:    config entries for the servers above
```

Everything reyn needs already exists: `embed` (tool), the MCP **client**, and pipeline `ToolStep`. **Zero reyn core change is the target**, and any deviation from that should be justified in review rather than assumed.

**Feasibility confirmed (architect co-vet, F-A)**: the `ForEach file` above is not aspirational — the pipeline DSL already has **`for_each` / `parallel` / `fold`** (`pipeline-dsl.md` §84-128). The largest potential zero-core blocker does not exist. (`fold` is also what X2a uses to aggregate spend.)

*(Note: reyn also ships its own MCP **server** — `reyn mcp serve`, exposing agents to Claude Code / Cursor via `src/reyn/mcp/server.py`. That is unrelated to this arc: here reyn is the **client** of bundled servers. Flagged only because an earlier framing wrongly described reyn as client-only.)*

## Component selection (OSS-first)

### 1. Parser — `markitdown-mcp` (Microsoft, official)

- Exposes **one tool**: `convert_to_markdown(uri)` (`file:` / `http:` / `https:` / `data:`).
- Covers the owner's **entire format list** (txt/md/pdf/xlsx/pptx/docx) plus 15+ formats.
- `uvx markitdown-mcp` → zero-install; also pip-installable (Python 3.10+). STDIO / Streamable HTTP / SSE.
- ~12 s per 100 pages; ~82 % F1 on independent benchmarks.

**Fallback (opt-in, not default): Docling (IBM)** — ~88 % F1, layout-aware (ML layout detection + optional GraniteDocling VLM), materially better on dense tables / multi-column / scanned PDFs, but **~2 min per 100 pages on CPU**, a heavy ML dependency, and ~6 formats. Excel/PowerPoint are already structured, so MarkItDown handles them well; Docling earns its cost mainly on **scanned or multi-column PDFs**. Default light, escalate on demand — carrying an ML layout model by default contradicts the minimal-dependency posture.

### 2. Chunker — chonkie (OSS), wrapped as MCP

- 10 chunker types in a **15 MiB** package (LangChain text-splitters ≈ 80 MiB, LlamaIndex ≈ 171 MiB); recursive chunking benchmarked at **3.54 MB/s** (semantic: 0.33 MB/s).
- 2026 industry default: **recursive, 256–512 tokens, 10–15 % overlap** covers ~80 % of cases. Semantic chunking only for clearly multi-topic documents.
- **`size` and `overlap` are pipeline INPUTS with defaults, not constants baked into a step.** The builtin is a template people copy and tune (R2); hardcoded numbers would make the first thing a user wants to change the hardest thing to find.
- **OPEN (survey first, per OSS-first)**: does a chunking MCP server already exist? If not, a **thin builtin wrapper** around chonkie — which is still "builtin content", not a reyn core change.

### 3. Vector-DB MCP — selection is gated by C1

**MUST accept externally-computed vectors.** A server that embeds internally (e.g. an sqlite MCP with a bundled Qwen embedder) **violates C1**: it ignores the user's configured `embedding_class` and splits the vector space between reyn's `embed` and the server's own model. **This is the primary rejection criterion, not a preference.**

The gate is **doubly derived** (architect co-vet): a server-internal embedder violates C1 *and* breaks **C4** — the per-chunk `embedding_model` column would record a model that never produced those vectors, i.e. the column becomes a lie and the "different model → different source" rule silently fails.

Requirements:
1. **Accepts pre-computed vectors** (C1 + C4) — **hard gate, pass/fail**.
2. **User-specified single sqlite file** (the owner's "出力ファイル名を指定").
3. **top-k + metadata filter** query.
4. **Metadata passthrough** — at minimum reyn's `ChunkMetadata` shape: `source_path`, `source_type`, `content_hash`, `embedding_model`, `chunk_index`, `size_tokens`, `parent_context`, `extra`.
5. **Generic store ops sufficient for a pipeline-owned diff**: **metadata-filtered listing without vectors** (Chroma's `get(where=…)` shape) + **upsert** + **delete**. *Relaxed from the draft's "hash-keyed add/update/remove native"* — see C5 ownership below.

Candidates to evaluate: **vectorlitedb** ("the SQLite for vector embeddings — everything in a single file"), **sqlite-vec**-based servers, **Chroma** embedded (`PersistentClient`, Apache-2.0). Prefer an existing OSS server; a builtin wrapper (e.g. thin sqlite-vec) only if none satisfies gate 1.

### C5 ownership — the **pipeline** owns the hash-diff (settled at co-vet)

The `content_hash` add/update/remove diff lives in the **ingest pipeline**, not in the vector-DB server. Three reasons:
1. **It widens the P1 gate.** Demanding native hash-diff semantics server-side shrinks the candidate set and raises the bar for every future backend the user might swap to. Pipeline-owned, the server only needs generic ops (requirement 5 above).
2. **It is exactly the part users should read.** The diff logic *is* the interesting template content — aligning with R2 (the builtin is the thing people copy).
3. **It keeps the arc portable.** Backend swap stays "re-point the MCP server", not "find a server that implements our diff semantics".

## Embedding cost tracking (owner requirement, 2026-07-15)

Ingesting a folder is a **batch spend**: many files × many chunks × an API-priced embedding model. This is precisely where the Product-Think lens ("cost-disciplined, legible to the operator") has to hold, and today it does not.

**Verified current state (primary-mapped):**

| Fact | Evidence |
|---|---|
| `embedding.cost_warn_threshold` exists — but it is a **token count** (default `10000`), not a dollar figure | `config/embedding.py:84` |
| It is consulted by the **in-core `index_update` only** — estimates via `EmbeddingProvider.estimate_tokens` on the to-embed batch **after** the pre-embed dedup skip, and on exceed emits an `index_update_cost_warning` (P6) + a `cost_warning` field in the envelope, without blocking | `core/op_runtime/index_update.py:35` (co-vet #4) |
| **`embed`'s result envelope already carries usage**: `{"vectors", "model", "total_tokens"}` — so a **pipeline can read spend directly**, even though `tools/embed.py` itself emits no cost event | `core/op_runtime/embed.py:88-113` (architect co-vet, independently grounded) |
| **No embedding price rate is resolved** — `pricing.py` handles completion rates only… | `llm/pricing.py` |
| …but **litellm's cost map already contains 110 embedding-mode rate entries** — so dollars are an *existing-lookup extension*, not a new rate table | architect co-vet (measured) |
| ∴ **embeddings never reach `CostBreakdown` / the status-bar cost panel / `project_cost_breakdown`** | #2931 / #2933 are built on completion usage |

**The asymmetry is backwards**: the path reyn drives for itself (in-core `index_update`) warns about cost, while the path where **the user ingests their own 10k-document folder and is actually billed** (`embed` → this arc's pipeline) is silent. A user can run the builtin ingest, be charged real money, and watch the cost panel read **$0.00**.

**Requirements for this arc (user-facing path):**

| # | Requirement | Home (settled at co-vet) |
|---|---|---|
| **X1** | **Pre-flight estimate before a batch embeds.** The ingest pipeline surfaces estimated tokens *before* spending, via a `transform` step using the chars≈4 approximation (reyn's established fallback). Batch ingest is exactly where a pre-flight matters — unlike a chat turn, the user cannot eyeball the size. | **This arc — builtin content, zero core** |
| **X2a** | **In-pipeline spend aggregation + display.** The ingest pipeline `fold`s `envelope.total_tokens` across chunks and reports the total in its result (+`present`). **This is the core of the owner's requirement — the user sees what the ingest cost — and it closes with zero core change.** | **This arc — builtin content, zero core** |
| **X2b** | **Embedding cost is tracked in reyn core, as its OWN independent aggregate** (owner: *"embedding は独立追跡の想定"*) — priced per call, available per scope (session / agent / project), **not folded into the chat `CostBreakdown`**. **Owner-required (2026-07-15): "embedding コスト追跡は reyn 本体側でできるようにしてね"**. **The status-chip *rendering* is explicitly a SEPARATE WAVE** (*"チップ表示自体は別 wave で良いよ"*) — this arc lands the **data**, correct and consumable; the panel wave renders it. | **REQUIRED — core PR (PC)**, backend only |
| **X2c** | **`embed`'s output metadata carries the cost**, not just `total_tokens`. Owner: *"embed tool 出力メタデータとして出せれば直良"* — cheap once X4 lands (the rate is resolvable at that point), and it is what the ingest pipeline reads for X2a. | **Core PR (PC)** |
| **X3** | **Reuse the established mechanism — do not invent a second.** `embedding.cost_warn_threshold` + a `*_cost_warning` audit-event + an envelope `cost_warning` field is already the in-core pattern (co-vet #4). Extend that shape; do not build a parallel scheme. | Applies to whichever PR touches it |
| **X4** | **Dollars — required**, since the status chip displays dollars. Not a new rate table: **litellm's cost map already has 110 embedding-mode entries**, so this extends the existing lookup to embedding mode. | **REQUIRED — same core PR (PC)** |
| **X5** | **Account AFTER dedup** (mirrors C5 + the in-core rule). Re-ingesting an unchanged folder must cost ≈0 **and report that it did** — the `content_hash` skip is the user's main cost lever, so it must be visible. | **This arc** (the pipeline owns the diff — see C5 below) |
| **X6** | **Mixed-model correctness.** Owner-required (2026-07-15). Once embeddings are tracked, **mixed models are unavoidable by construction** — the embedding model is never the chat model, and #2934 added per-agent-step model overrides on top. **Price each call at ITS OWN model's rate at call time, then aggregate dollars.** Never aggregate tokens across models and price them afterwards at some session-level model — that is the failure mode this requirement exists to prevent. | **Core PR (PC)** — follow the existing invariant, do not bypass it |

**Scope resolution.** The draft framed cost tracking as the one thing that might breach "zero core change"; **F-B softened that** (X1 + X2a are reachable as builtin content, since `embed`'s envelope already returns `total_tokens`/`model`). **The owner then settled the rest deliberately (2026-07-15): embedding cost tracking is to be done in reyn core, and the status-chip cost must show it.** So this arc has **exactly one core PR (PC = X2b + X4 + X2c)** — a scoped, intentional exception to the zero-core target, not a discovered breach. The in-core `index_update` path keeps its own warning and stays out of scope.

**F-B also settles C4 for free**: the pipeline can stamp chunk metadata from `envelope.model`, so "one source = one embedding model" needs no core change either.

### Embedding cost is tracked INDEPENDENTLY (owner decision, 2026-07-15)

**Decision: *"embedding は独立追跡の想定だよ"*** — embedding spend is its **own** tracked aggregate, **not** a component folded into the chat `CostBreakdown`.

**Why this is the right call, and why it was a real fork.** `CostBreakdown` (#2931) models a **chat** call: `prompt` (non-cached input) / `cache-read` / `cache-creation` / `completion`, plus derived `cache_savings` and `cache_hit_rate`. An embedding call fits none of it — it is *input-only*: no completion, no cache read/creation, no savings, and **structurally uncacheable**. Had embeddings been mapped onto `prompt`:
- ingest spend would be indistinguishable from chat-input spend, and
- **`cache_hit_rate` / `cache_savings` would be diluted** — their denominators absorbing tokens that could never have been cached, quietly making the cache look worse than it is. Those figures are documented as *"backend-only; no panel UI yet"*, so the corruption would have been **in the data, today**, unaffected by the chip-display deferral.

**Consequences of independent tracking:**
- The chat `CostBreakdown` is **untouched** — its components-sum-to-total invariant and its savings math stay exactly as #2931 pinned them, by construction rather than by careful exclusion.
- **"How much did embedding cost?"** is directly answerable per scope (session / agent / project).
- The later chip wave can render it as its own figure **or** fold it into a grand total — its choice, precisely because the data arrives already separated. It never has to reverse-engineer the split.
- **X6 still applies *within* embedding tracking**: per C4, different sources may use different embedding models, so a project routinely has **multiple embedding models in flight**. Each embedding call is priced at **its own** model's rate and aggregated as dollars — the same invariant as chat, applied inside the embedding aggregate.

## Risks

### ⚠️ R1 — `uvx` zero-install repeats the FP-0057 line-55 firewall failure

FP-0057 recorded that the default embedding downloads ~22 MB from HuggingFace on first use, and **in the owner's corporate firewalled network HF is blocked**, degrading the index build. **`uvx markitdown-mcp` fetches from PyPI on first run — the same failure class**, and it would strike at *step 1* of the builtin pipeline. This is not hypothetical; it is the owner's actual environment. Decide explicitly: vendor the servers, pre-warm the cache, or document + **fail loudly with a decision-enabling message** rather than degrade silently (Reliability lens). A turnkey feature that dies behind a corporate proxy is not turnkey.

### R2 — Readability is a first-class requirement, not polish

The builtin pipeline **is** the extension mechanism: swapping backends means copying it and re-pointing the MCP server. Write it plainly — no clever indirection — because users read it as the template. This constrains implementation style, so it belongs in the review checklist.

### ⚠️ R3 — Permission posture: the builtin mcp config × #2932 auto-grant intersection

Bundled MCP servers ride the per-server permission gate (`permissions.mcp.<server>`). markitdown reads **arbitrary paths**; the vector-DB writes a user-named file. But there is a **specific hazard the draft under-stated** (architect co-vet, F-D):

**#2932 grants configured MCP servers automatically on `reyn pipe run`.** Its justification is *trusted-by-configuration*: the operator **explicitly** configured that server **and** explicitly ran the pipe. **If a builtin-shipped mcp config counts as "configured", that premise silently evaporates** — shipping the builtin would mean that the first `reyn pipe run` auto-grants markitdown's arbitrary-path read, with no operator decision anywhere in the chain.

**Requirement: the builtin mcp config ships INERT** — as a sample / commented-out entry that the operator must explicitly enable. This preserves #2932's premise (explicit configuration = the operator's decision) instead of letting a builtin quietly satisfy it. Note the precedent: builtin **skills** already ship inert (`force_visibility_on_demand`, "A3 inert-ship" in `builtin/registry.py`; the stamp was `force_auto_invoke_false` until #2971 renamed the axis) — the same posture, for the same reason.

### R4 — Chunk defaults are a quality lever, and 0057's number is for a different case

FP-0057 line 51 suggests recursive ~800–1024 tokens + overlap, but that guidance is explicitly for **throwaway ephemeral attachments** ("used-once → low chunk-quality requirement"). User RAG is **persistent and quality-sensitive**, where the 2026 default is **256–512 + 10–15 % overlap**. Do not inherit the 800–1024 number by accident — reconcile deliberately.

## Not in scope

- **Status-chip cost rendering** — owner-deferred to a **separate wave** (2026-07-15: *"チップ表示自体は別 wave で良いよ"*). This arc lands the **data** (X2b/X4/X2c/X6, backend); that wave renders it. The `CostBreakdown`-fit decision above still belongs here, because it is a data-model question and the savings figures are already backend-only.
- **#2944 item 1** (query-time freshness in the retrieval contract) targets the **in-core** `IndexBackend.query()`. The user store is external (C2), so it is **not** this arc. Keep separate.
- Automatic ingestion / file-watch reindex (FP-0057 defers this to a future automation arc).
- Reranking / hybrid / ANN.

## Phasing

| Phase | Content | Gate |
|---|---|---|
| **P1** | **Selection spike**: survey for an existing chunking MCP; verify a vector-DB MCP satisfying **C1 (pre-computed vectors — pass/fail)** + user-specified sqlite path + the **relaxed** gate 5 (metadata-filtered listing without vectors + upsert + delete) | C1 is pass/fail; fallback = thin sqlite-vec wrapper (builtin content), so the arc is bounded either way |
| **P2** | builtin MCP config entries (+ the chunker wrapper if P1 finds none) | **Ships INERT** (R3 / F-D) — operator must explicitly enable, preserving #2932's trusted-by-configuration premise |
| **PC** | **Core PR — X2b + X4 + X2c + X6 (backend only)**: embedding usage priced at **its own model's rate** → `CostBreakdown` / `project_cost_breakdown`; dollars via litellm's existing embedding rates; cost in `embed`'s output metadata | **Owner-required.** The only core change in the arc; sequence **before/with P3**. **Acceptance = the tracked data is correct and consumable** (per-scope aggregates + embed metadata), **NOT** a rendered chip — **the status-chip display is a separate wave** (owner). Must first settle the `CostBreakdown` fit (above): embeddings must not dilute the cache-savings math. Extends #2931; architect co-vets |
| **P3** | builtin **ingest** pipeline (incl. **X1** pre-flight, **X2a** `fold` spend total, **X5** post-dedup accounting, **C5** hash-diff, **C4** stamp from `envelope.model`) + **query** pipeline | Readability (R2); **zero core change** — feasibility confirmed by F-A (`for_each`/`parallel`/`fold` exist) |
| **P4** | builtin **skill** + docs, including "how to swap the backend by copying this" | Doc surface complete; feature-map entry lands here (docs-maintainer: impl-extracted convention) |
| **P5** | Offline/firewall posture (**R1**): step-0 reachability check → decision-enabling error + documented pre-install path | Owner's own network is the acceptance test |

## Architect co-vet — verdict and resolutions

**Verdict: direction GO** (reviewed at draft sha `c88f9258`; all code claims independently re-grounded on `b375aaec`, not taken from this doc's citations). The six open questions are **resolved and folded above**:

| # | Question | Resolution |
|---|---|---|
| 1 | Chunking MCP — existing OSS or thin wrapper? | **Survey first**; wrapper fallback is within the owner's frame (builtin content) |
| 2 | Is the C1 hard gate a valid derivation? | **Valid, and doubly so** — a server-internal embedder breaks **C4** as well (the per-chunk `embedding_model` column becomes a lie). C2 non-violation confirmed: the pipeline only calls MCP tools; zero reyn-side store code |
| 3 | Chunk defaults 256–512 vs 800–1024? | **256–512 + 10–15 %**. 0057 line 51 self-declares its number is for "throwaway / used-once → low chunk-quality requirement" (verbatim), so non-inheritance is textually grounded. **Plus: make size/overlap pipeline inputs** |
| 4 | R1 firewall — vendor / pre-warm / fail-loud? | **fail-loud + a documented pre-install path.** Vendoring contradicts the wheel-size / minimal-dependency posture; pre-warm is environment-dependent. Step 0 of the pipeline does a **server reachability check → decision-enabling error** (precedent: #2932's `require_mcp` error = cause + concrete remedy). **The owner's network is the acceptance test** |
| 5 | C5 ownership — pipeline or server? | **Pipeline owns the hash-diff** → gate 5 relaxes to generic ops, widening P1 candidates. Folded above |
| 6 | X2/X4 core-change breach — this arc or split? | **Split, but on a different line than the draft assumed** — F-B showed X1+X2a are achievable as builtin content with zero core, so only X2b+X4 become a small core PR. Folded above |

**Findings folded**: F-A (DSL `for_each`/`parallel`/`fold` exist → zero-core feasible), F-B (`embed` envelope already carries `total_tokens`/`model` → re-split X, and C4 stamping is free), F-C (litellm has 110 embedding rate entries → X4 is cheap), F-D (builtin mcp config × #2932 auto-grant → ship inert).

## Remaining open questions (for owner / P1 spike)

1. **Does an OSS vector-DB MCP satisfy the C1 hard gate + user-specified sqlite path + the relaxed gate 5?** This is the P1 pass/fail; the fallback (thin sqlite-vec wrapper as builtin content) is defined, so the arc is not open-ended either way.
2. **Does an OSS chunking MCP exist**, or do we ship a thin chonkie wrapper?

## Sources (component survey, 2026-07)

- MarkItDown MCP: [PulseMCP](https://www.pulsemcp.com/servers/markitdown) · [microsoft/markitdown — packages/markitdown-mcp](https://github.com/microsoft/markitdown/tree/main/packages/markitdown-mcp)
- Document-processing MCP servers: [ChatForest — MarkItDown vs Docling vs Kreuzberg](https://chatforest.com/guides/best-pdf-document-processing-mcp-servers/)
- Parser comparison: [file2markdown — Docling vs MarkItDown](https://www.file2markdown.ai/blog/docling-vs-markitdown) · [danilchenko.dev — MarkItDown vs Docling vs Marker](https://www.danilchenko.dev/posts/markitdown-vs-docling-vs-marker/)
- Chunking: [Firecrawl — Best Chunking Strategies for RAG in 2026](https://www.firecrawl.dev/blog/best-chunking-strategies-rag)
- Vector store candidates: [vectorlitedb](https://github.com/vectorlitedb/vectorlitedb) · [MCP.Directory — vector DB comparison 2026](https://mcp.directory/blog/chroma-vs-pinecone-vs-qdrant-vs-weaviate-vs-pgvector-mcp-2026)
