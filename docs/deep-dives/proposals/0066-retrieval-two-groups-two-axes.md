# FP-0066 — Retrieval redesign: two groups (action / knowledge) × two axes (scheme × transport)

**Status**: Proposed — owner 壁打ち design-of-record (2026-07-24/25). Awaiting owner GO to phase-implement.
**Supersedes / corrects**: the in-core-vs-user framing of FP-0057 (0057), the file.read-subsumed skill-load of #2971, and PR #3240 (reversed here).
**Related**: FP-0034 (universal catalog / action retrieval), FP-0063 (builtin user RAG — **unchanged**), ADR 0064 §3.5 (skill-load verb), #3196 (skill-body symlink surface), #3026 (enumerated-action-set invariant).

---

## Summary

reyn's RAG splits **by audience**: **user RAG** (user documents → the user's own external vector store, via the FP-0063 plugin — **out of scope here, unchanged**) and **internal-consumption RAG** (reyn embeds its OWN signals and retrieves them at OS level). This proposal redesigns the **internal** side around two orthogonal structures the owner 壁打ち surfaced:

1. **Two axes.** Tool-use decomposes into **scheme (presentation — how capabilities are shown/discovered: `enumerate-all` / `category` / `retrieval`)** × **transport (how an action is expressed — `tool-calls` / `structured-output` / `content-fence`)**. Today these are conflated: `codeact` is registered as a 4th "scheme" but is really the **content-fence transport**. The refactor separates the axes.

2. **Two groups.** Everything retrievable falls into **action** (tool / mcp / pipeline — *callable*, `invoke → result`) or **knowledge** (skill / memory / repo_doc / repo_src — *loadable*, `load → context`). Discovery is unified per group; **consumption stays kind-specific**. This resolves the "heterogeneous retrieval-result presentation" problem: two internally-uniform groups instead of one mixed result.

The internal store (`SqliteIndexBackend`) becomes **OS-only**; the agent-facing in-core RAG tools (`semantic_search` / `index_update` / `drop_source` / `list_rag_sources`) are **retired (clean-break)** — they were a pre-audience-split relic. The substrate (embed funnel + `SqliteIndexBackend`, already unified by #2843/#2856) is kept as OS-internal primitives.

---

## §1 Background — why this exists

FP-0057 split RAG **by audience** and shipped both halves, but the **in-core half's agent surface** was built before the split settled: the agent could `index_update` its own docs into reyn's store and `semantic_search` them — *user-RAG semantics on the OS-internal store*. After the pivot (user RAG → FP-0063 plugin / external store), those agent tools are orphaned. The owner's original question ("why do the old rag_operation tools remain?", #3222) is answered here: **retire them; the in-core store is for OS internal consumption only.**

Separately, the "internal consumption" use-case (reyn retrieving its own tool-catalog / skills / memory / docs) was **under-designed**. It exists only partially: `search_actions` + `ActionEmbeddingIndex` (opt-in via `action_retrieval.embedding_class`) retrieve the **tool/action catalog**; FP-0010 "RAG routing" folded into that; **skill / memory / repo are not retrievable today**. This proposal completes it.

---

## §2 Two axes: scheme (presentation) × transport (expression)

Current `_SCHEMES` registry has 4 entries (`enumerate-all`, `universal-category`, `retrieval`, `codeact`) that each own **both** presentation and the response→action transport (`scheme.py:5`). Clean model — two orthogonal axes:

| axis | values | meaning |
|---|---|---|
| **scheme (presentation)** | `enumerate-all` / `category` / `retrieval` | how capabilities are shown & discovered |
| **transport (expression)** | `tool-calls` / `structured-output` / `content-fence` | how the model expresses the chosen action |

- **`codeact` is not a scheme — it is the `content-fence` transport** (writes a ```python fence in `content`; no `tools=`). It gets re-placed onto the transport axis.
- `tool-calls` (enumerate/category/retrieval today) and `content-fence` (codeact) are implemented; **`structured-output` (`response_format`/json_schema) is currently used only for schema-constrained answer/eval turns (#0062), NOT as a tool-use transport** — it is a legitimate future third transport (not wired here).
- **Config**: the operator selects **both** `tool_use.scheme` × `tool_use.transport` (clean-break from the conflated `tool_use.chat`). Presentation (retrieval index) is transport-agnostic.

Scheme selection is **static** (config-time): the configured capability set is known, so runtime auto-switching is unnecessary — **#1834 (dynamic enumerate⇄discovery switch) is dropped**.

---

## §3 Two groups: action vs knowledge

| group | members | consumption | question |
|---|---|---|---|
| **action** | tool / mcp / pipeline | **invoke → result** (callable) | "what can I DO" |
| **knowledge** | skill / memory / repo_doc / repo_src | **load → context injection** (loadable) | "what do I KNOW / can reference" |

The distinguishing axis is *callable vs loadable* — the same split MCP draws (tool vs resource) and the competitor draws (Anthropic Tool Search Tool = tools only; skills/memory are separate systems). **Unified discovery, kind-specific consumption**: this is why one mixed retrieval result was awkward — the two groups have fundamentally different activation. `events` is **not** a retrieval source (it fell out of the knowledge group during design).

---

## §4 Retrieval index

- **Substrate is already unified** (#2843/#2856): one `embed` funnel (`EmbeddingProvider` → LiteLLM, sole embedder) + one `SqliteIndexBackend`. `ActionEmbeddingIndex` is a thin adapter; the action catalog is source `"actions"`. **No new framework.**
- **Per-kind source split** (S2): replace the single whole-catalog-hash `"actions"` source with **one source per kind** (`tool` / `mcp` / `pipeline` / `skill` / `memory` / `repo_doc` / `repo_src`). Ingest scope, incremental reconcile, and de-index all close per source. `action_index.py:89` already anticipates this; the `ActionEmbeddingIndex` adapter retires into the per-kind path.
- **Chunk granularity**: **per-entity for action** (1 tool = 1 chunk); **classic-RAG chunking for knowledge** (all md-ish; §G7: repo_src is code → v1 uses plain text-chunk, code-aware chunking is a future ticket).

---

## §5 Retrieval contracts: search_actions / search_knowledge

Two search verbs (owner: separate, not one param) — symmetric but not identical:

```
action    : search_actions  → describe_action(args schema) → invoke_action(name, args)   [3-step; args schema needed]
knowledge : search_knowledge → (kind-specific load verb)(id)                              [2-step; id is the load arg]
```

- **`search_knowledge(query) → [(kind, id, title, description), …]`**. `id` is the kind-native identifier passed straight to the kind's load verb: skill→skill name, memory→doc path, repo_doc/repo_src→doc/source path. No abstract handle.
- **§G1 (chunk→entity aggregation)**: the index is chunk-level (classic RAG) but the result is **entity-level**. `search_knowledge` = "search chunks → aggregate to entity (max-score / dedup) → return one row per entity." Must be specified explicitly.
- **Activation is kind-routed**: action → `invoke_action`; knowledge → the kind's load verb (skill→`load_skill`, memory→`read_memory`, repo→repo read). No unified load verb (see §6 — a unified one re-creates the file.read smell).

---

## §6 skill-load: a dedicated verb (strip file.read)

**Corrects a design-vs-impl drift.** ADR 0064 §3.5 (#3070) called for a **skill-load verb**; #2971 instead chose "reading IS the invocation, no dedicated verb" and routed skill-load *inside* `file.read` via a special path (`is_skill_body_path`). Consequences: **skill-specific logic is scattered through `file.py`** (the `is_skill_body_path` / `load_skill_body` / `skill_body` identifiers recur a dozen-plus times — exact count is grep-method-dependent, so the anchor is the *presence*, not a number) — provenance classification, `${env}`-expansion trust-gating, and the **#3196 symlink-swap security surface that exists only because the provenance-check and the read were conflated**. This is the same "foreign responsibility riding an op" anti-pattern the owner already fixed for `plugin_install`/venv (§3.11b / #3209).

**Decision (ratified here to prevent re-drift):** extract a first-class **`load_skill`** verb owning provenance / `${env}` expansion / permission / the symlink-swap-safe resolve; `file.read` returns to plain file reading (the scattered special-cases + the skill-load import removed). This is **P0** — independent, immediately valuable, and the prerequisite for skill as a first-class knowledge-group entity.

---

## §7 opt-in: `embedding.enabled`, symmetric model

**Amended by #4156** (2026-08, "future ticket" above landed sooner than expected — an owner TPM incident forced it): the "single switch, no granularity" model this section describes as v1 is no longer reyn's actual behavior. `embedding.enabled` stayed the provider/cost gate, but WHAT it turns on split into `embedding.index.actions` (the ~10-entry action catalog, default **on** — the v1 behavior below, unchanged for an operator who never touches this field) and `embedding.index.repo_knowledge` (the FP-0066 P3b whole-repo knowledge index this section's own "knowledge-retrieval" bullet below refers to, default **off** — the workload that caused the incident). This is exactly the granularity this section called YAGNI; the incident is why it stopped being YAGNI. This section's own body is left as the v1 design-of-record and not edited in place — see `docs/reference/config/reyn-yaml.md`'s `embedding` fields table for the current, accurate shape.

- **Single switch `embedding.enabled: bool = false`** (default OFF). ON → action-retrieval + knowledge-retrieval + the plugin's embed step. Clean-break replacement for the fragmented gates (`action_retrieval.embedding_class` splits into `embedding.enabled` [on/off] + the embedding class [which model, default `standard`]). No global/group/source granularity (YAGNI; future ticket).
- **Default-off rationale**: embedding needs a provider + cost, so off is the predictable/safe default (owner's opt-in/predictability principle) — this holds independent of competitor behavior (Anthropic tool-search is on-by-default, but that is the defer+search mechanism, not embedding-specifically).
- **Symmetric model — embedding gates only the semantic-discovery layer, never the features:**

| layer | needs embedding? | when OFF |
|---|---|---|
| access / load (`load_skill`, `read_memory`, `invoke_action`) | no | works |
| non-semantic discovery (`list_actions`, `list_skills`, …) | no | works |
| **semantic discovery** (`search_actions` / `search_knowledge`) | yes | hidden |

`list_*` is complete non-semantic discovery; `search_*` is the opt-in semantic *enhancement*. OFF degrades UX, never removes capability.
- **§G9 (plugin touchpoint)**: OFF → the FP-0063 `rag_ingest` embed step must pre-flight and return a **decision-enabling block** ("set `embedding.enabled: true`"). Only contact point with the otherwise-unchanged plugin.

---

## §8 ingest: dynamic sync-in-op / static background / search guarantees completeness

Primitive: the **`index_update` op** (incremental content_hash reconcile: add/update/remove/skip) — kept as an **OS-internal** primitive (see §9). Incremental reconcile applies to **dynamic** sources (skill/memory/mcp); **static repo (`repo_doc`/`repo_src`) uses full-replace in v1** (`mode="replace"`, a whole-source re-embed per build) — repo-incremental is a deferred future ticket (§12), since the v1 enable-time-only trigger rarely reaches a second build where an incremental skip would pay off. An **IndexCoordinator** (S1) owns dirty-marking, the background build queue, cross-process `build_lock`, and search-time await — so the await logic is **not scattered** across install/remember/search (which would re-play the file.read dispersion). `SourceManifest` carries dirty/pending state.

- **dynamic** (skill/pipeline install, memory remember, mcp tool-list fetch/refresh) → **sync-complete inside the op** (await). The mcp tap points already exist: `mcp_list_tools` on connect + `maybe_refresh_mcp_tools_from_yaml` change-detection (FP-0037).
  - **§G2**: sync-in-op ingest is **best-effort** — a provider failure must NOT fail the install/remember; leave a **dirty mark**, and let search-await-pending heal it. (The completeness guarantee below IS the recovery path.)
  - **§G3 (delete side)**: `forget` / skill·pipeline uninstall / mcp server drop → **synchronous de-index** (content_hash reconcile handles remove). Symmetric with the add side.
- **static** (tool builtin, repo_doc/src) **+ backfill** (entities that pre-existed `embedding.enabled=true`) → **background build** (existing `asyncio.create_task` pattern), **never a foreground surprise** on an unaware operation. Trigger for repo is **enable-time (re-index)** only — v1 builds once when `embedding.enabled` flips on (or on an explicit re-index), not on an ongoing detection of repo-content changes; a repo-change live-follow trigger (filesystem watcher / mtime-based dirty-marking) is a deferred future ticket (§12), so repo knowledge is a deliberate **enable-time snapshot**, not a continuously-current index like the dynamic sources above.
- **search guarantees completeness** (owner: *best-effort search is a bug*): `search_*` **awaits any pending/dirty ingest to completion before returning**. Steady-state = a cheap hash/count no-op (dynamic kept current by sync-in-op). Cold-start (post-enable / large static change) → the racing search **waits for the build** — acceptable because *search is a user-aware point and the user deliberately enabled embedding*.
- **audit-event phase emit** (for the deferred UX): emit `embedding_index_build_started/_progress/_complete` + `semantic_search_started/_complete` from the start (Observability lens), so the TUI ingest-vs-search chip is a pure additive follow-on.

---

## §9 retire: (A) full clean-break

| layer | disposition |
|---|---|
| **1 — agent-facing LLM tools** | **retire**: `semantic_search`, `index_update`, `drop_source`, `list_rag_sources`; **reverse #3240**. Replaced by search_actions/search_knowledge + invoke_action / load verbs. |
| **2 — user-facing in-core source creation** | **retire**: safe-mode `index_update()` (user) + CLI `reyn source`. User RAG = FP-0063 plugin only. |
| **3 — OS-internal ops / substrate** | **keep**: `IndexUpdateIROp` / `SemanticSearchIROp` / index_query / index_drop / embed ops, `IndexBackend` / `SqliteIndexBackend`, `EmbeddingProvider` — used by §8 ingest + §5 search. |

in-core store = **OS-only**; no compat shim, no migration (clean end-state). **§G8**: the #3026 "enumerated-action-set is a constant, no operator-minted actions" invariant must be **re-worded to "the catalog *enumeration* is constant"** — the retrieval index legitimately carries operator-derived dynamic names (mcp tool / pipeline names); dispatch already accepts them (`pipeline__<name>` = dispatchable-but-not-enumerated), so only the invariant's wording drifts (doc-sync hard rule).

---

## §10 Review-findings resolution (this-arc, G1–G9)

| finding | resolution | section |
|---|---|---|
| G1 chunk→entity aggregation | specified in search_knowledge contract | §5 |
| G2 sync-in-op failure semantics | best-effort + dirty; search-await heals | §8 |
| G3 delete-side triggers | forget/uninstall/drop → sync de-index | §8 |
| G4 knowledge auto-inject in `retrieval` scheme | **future ticket** — v1: knowledge is on-demand only | §12 |
| G5 transport×scheme config | 2-key `tool_use.scheme` × `tool_use.transport` | §2 |
| G6 repo per-workspace embed duplication | **future ticket** — version-keyed shared cache | §12 |
| G7 repo_src not md | v1 text-chunk; code-aware = future | §4, §12 |
| G8 #3026 invariant wording | re-word to "enumeration is constant" | §9 |
| G9 plugin × embedding.enabled | decision-enabling pre-flight block | §7 |

---

## §11 Phasing (this arc = one umbrella issue)

- **P0** — extract `load_skill` verb; strip the scattered skill special-cases from `file.py` (independent, immediate; shrinks #3196 surface).
- **P1** — retire layers 1+2 (reverse #3240) + config clean-break (introduce `embedding.enabled`; fold-remove `mcp_search_threshold` #3218). LLMReplay fixtures re-recorded (RED-pairing). Doc rewrites: `docs/concepts/data-retrieval/rag.md`, feature-map, reyn-dir-layout.
- **P2** — per-kind source split + sync-in-op ingest + **IndexCoordinator** + audit-event phase emit.
- **P3** — `search_knowledge` + knowledge ingest (skill/memory/repo) — depends on P0's `load_skill`.
- **P4** — scheme×transport 2-axis refactor (largest, most independent — last); re-place codeact as content-fence transport.
- UX ingest-vs-search chip: additive follow-on after P2.
- Verb naming (`search_knowledge`, `load_skill`, qualified forms) ratified with the #3223 naming-convention arc (S4).

---

## §12 Out of scope — future tickets (separate)

- **G4** — knowledge auto-injection in the `retrieval` scheme (v1: on-demand `search_knowledge` only).
- **G6** — repo_doc/repo_src version-keyed per-user shared embedding cache (`~/.reyn/cache/index/repo@<version>/`, mirrors ADR 0064 §3.3 global-cache) to avoid per-workspace embed duplication.
- **code-aware chunking** for repo_src (v1 = plain text-chunk).
- **repo incremental ingest**: repo (`repo_doc`/`repo_src`) builds use `mode="replace"` (full re-embed) in v1, bound to the enable-time build; incremental content_hash-skip reconcile for repo is deferred — it only benefits a SECOND build, which the v1 enable-time-only trigger rarely reaches.
- **repo staleness / repo-change trigger (live-follow)**: repo knowledge is an **enable-time snapshot** in v1 — file changes on disk are NOT auto-reflected until a re-index / re-enable. A live-follow trigger (filesystem watcher, or lightweight mtime-based dirty-marking) is deferred. This is a deliberate, ratified **exception to the "best-effort search is a bug" completeness principle** (§8), scoped to repo (static) only — dynamic sources (skill/memory) stay sync-in-op current and are unaffected.
- **search UX** ingest-vs-search TUI chip presentation (owner deferred; audit-events emitted from P2 so this is purely additive).
- **`structured-output` as a tool-use transport** (the third transport; currently answer/eval only).
- **group/source-specific opt-in** (v1 = single `embedding.enabled`; YAGNI).
