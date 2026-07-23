---
type: concept
topic: architecture
audience: [human, agent]
---

# Charter — eight lenses × seven feature families

The full, populated companion to the constitution skeleton in `CLAUDE.md` (§ Constitution).
Where the constitution states each lens's one-line pass-line, this page grounds every
lens against reyn's actual implemented features — the canonical inventory is
[`docs/feature-map.md`](../../feature-map.md), and every non-empty cell below cites a
feature-map `file:line` as its exemplar.

## How to read this table

- **Rows** = the eight engineering lenses (see `CLAUDE.md` for each lens's pass-line).
- **Columns** = seven feature families, each a grouping of `feature-map.md`'s `###`
  sections (see [Family → feature-map section map](#family-feature-map-section-map)
  below).
- **Each cell** = that family's exemplar implementation of that lens, with a
  `feature-map.md` file:line citation. An empty cell is written as **"—"** — a lens
  genuinely does not manifest *in that specific family*. **"—" is not "this lens is
  covered better elsewhere"** — a lens can (and should) have a different exemplar
  in every family it genuinely shows up in; check the family's own feature-map
  section before writing "—", don't reach for a cross-family analogy or a
  same-word-different-meaning cousin (e.g. the Retrieval *lens*, about context, is
  not the `retrieval` tool-use *scheme*, about tool-surface scaling — don't conflate
  them). Cells are never invented to fill a gap; a lens that is honestly thin
  (Retrieval, Evaluation) will show mostly "—" across most families, and that
  sparseness is itself informative, not a defect in the table.
- **Don't conflate a band member with the lens that names it as a discipline.**
  `cost/budget (bounding)` is a cross-cutting band member — hard caps, refuse-on-exceed,
  the universal spend guard every feature respects. **Product Think** is a lens about
  legibility and predictability *for the operator* — cost *reporting*, warnings, and
  reduction (e.g. `present`'s ~0-token routing), never the bounding mechanism itself.
  The owner drew this line explicitly (bounding ≠ reduction/legibility); a Product
  Think cell that cites a refuse-on-exceed cap is citing the band, not the lens — find
  the family's actual reporting/warning/reduction exemplar instead. The same discipline
  applies to the other two band↔lens pairs (Security↔`permission`,
  Reliability↔`crash-recovery (WAL)`): the band member is the mechanism every feature
  must obey; the lens is the discipline of doing that mechanism *well* for its own
  purpose, which usually has its own, narrower exemplar.
- **A single feature can legitimately exemplify more than one lens if it has more
  than one facet.** e.g. the inline CUI's audit chips are both an **Observability**
  exemplar (the audit-trace-surface facet — the same operator-visible trace the P6
  event log records, surfaced live rather than replayed after the fact) and a
  **Product Think** exemplar (the operator-legibility facet — predictable, at-a-glance
  cost/agent/permission state). This is not the band↔lens conflation above (one
  mechanism, one true owner) — it's one feature with two genuinely distinct facets,
  each independently satisfying a different lens's pass-line. Don't force a single
  citation to pick one lens when the constitution's own pass-line (`CLAUDE.md`) names
  the mechanism for both.
- Authoring proceeds family-by-family (one PR per column). Not-yet-authored
  columns are marked **"*(pending)*"**, distinct from a deliberately-empty "—" cell
  within an authored column.

## The 8×7 grid

| Lens | Decision & Tool-Use | Chat & Session | Context & Retrieval | Orchestration | External surfaces | Safety & Config | Product surface |
|---|---|---|---|---|---|---|---|
| **System Design** | The agent loop is an OS-enforced contract: every side effect is a schema-validated, typed Control IR op, never a free-form string ([`feature-map.md:249`](../../feature-map.md)) | Human-in-the-loop is a first-class, surface-agnostic primitive — a permission ask or `ask_user` routes to the operator identically whether the agent runs in the inline CUI, CLI, web/A2A, or MCP ([`feature-map.md:618`](../../feature-map.md)) | Skills' three-layer exposure (L1 system-prompt menu → L2 on-demand `SKILL.md` read → L3 bundled-asset file-read) delivers context progressively at the layer that needs it, never stuffed unconditionally into the prompt ([`feature-map.md:533`](../../feature-map.md)) | Pipeline is a deterministic, Turing-incomplete control-plane DSL, not another agent loop — composition primitives are structurally closed (no nested launch, no arbitrary recursion) ([`feature-map.md:564`](../../feature-map.md)) | `EnvironmentBackend` abstracts repo-FS read/write/exec away from the OS + permission layer — the same governance layer applies whether the repo lives on the host (production) or in a container (⚗ Stage 2, experimental) ([`feature-map.md:712`](../../feature-map.md)) | Config hot-reload's IN-set/OUT-set file-split is the structural write-gate: hot-reloadable config lives in one set, security/budget/loop-valve config is restart-only in another — right layer decides what can change live ([`feature-map.md:412`](../../feature-map.md)) | Each CLI subcommand owns exactly its own subsystem's operator surface (agent/topology/memory/permissions/events/mcp/config/…) — no cross-cutting mega-command ([`feature-map.md:365-379`](../../feature-map.md)) |
| **Tool Contract** | Op kinds mirror `OP_KIND_MODEL_MAP` 1:1 in `schemas/models.py`, one typed schema per kind ([`feature-map.md:301`](../../feature-map.md)); every tool-use scheme dispatches through the same exclude → permission → dispatch gate regardless of presentation ([`feature-map.md:357`](../../feature-map.md)) | — | `semantic_search` (FP-0057 Phase 2a; renamed from `recall`) is a typed, schema-validated Control IR op (`SemanticSearchIROp`), never a free-form retrieval string ([`feature-map.md:499`](../../feature-map.md)) | A Pipeline `tool`/`agent` step's result is validated against a `SchemaRegistry`-backed nested schema (`verify: schema`) — never an untyped free-form return ([`feature-map.md:551`](../../feature-map.md)) | MCP resource reads (`mcp_read_resource`) are a typed Control IR op gated by the negotiated `resources` capability on the same axis as `mcp` itself — no bespoke per-server protocol bypasses the typed dispatch gate ([`feature-map.md:518`](../../feature-map.md)) | `SandboxPolicy` is a typed envelope for `sandboxed_exec` — `network` / `read_paths` / `write_paths` / `subprocess` / `env_passthrough` / `timeout`, never a bare shell string ([`feature-map.md:699`](../../feature-map.md)) | — |
| **Retrieval** | `semantic_search` (renamed from `recall`): per-source-model embed query → `index_query` per source → merge top-K ([`feature-map.md:318`](../../feature-map.md)) — context-retrieval, not to be confused with the `retrieval` tool-use *scheme* ([`feature-map.md:354`](../../feature-map.md)), which retrieves tools, not context | — | `semantic_search` + a pluggable `IndexBackend` a safe-mode Python step can call directly (`index_update()` — FP-0057 Phase 2b, retired `embed_and_index()` clean-break) — a RAG *framework* to build on, not a fixed pipeline; this is one of the constitution's two honest thin areas ([`feature-map.md:503`](../../feature-map.md)) | — | — | — | — |
| **Reliability** | Crash Recovery: `.reyn/` recovery-core classification, WAL state log, forward-replay resume, `CommittedStep` memo ([`feature-map.md:207-217`](../../feature-map.md)) | Multi-session crash recovery: on restart the full name → {sid → Session} structure is reconstructed from the WAL + snapshots, not just one conversation ([`feature-map.md:631`](../../feature-map.md)) | `skill_management__install_local`/`install_source` are threat-scanned and permission-gated, with config-generation recorded for crash-recovery — the reliability facet of the same install path ([`feature-map.md:537`](../../feature-map.md)) | Pipeline crash recovery: a per-run work-order persisted before step 0, step-boundary generation snapshots give exactly-once, truncation-surviving resume (including mid-`call`/`fold`/`for_each` state) ([`feature-map.md:560`](../../feature-map.md)) | MCP resource subscriptions: the runtime-only subscribed-URI set survives a transport-death reconnect, re-subscribing with a synthetic `resync` per URI rather than silently dropping state ([`feature-map.md:519`](../../feature-map.md)) | Force-close wrap-up: a denied limit gets the LLM one final tool-less turn to summarise what was accomplished rather than hard-stopping or looping unbounded ([`feature-map.md:442`](../../feature-map.md)) | Crash-durable cap counters: every cap counter is reconstructed on startup from the fsync-per-append ledger — the ledger, not the best-effort state-file cache, wins on recovery ([`feature-map.md:480`](../../feature-map.md)) |
| **Security** | `present`'s `data_ref` read authority resolves identically to `file.read` ([`feature-map.md:333`](../../feature-map.md)); `sandboxed_exec` runs under a declared `SandboxPolicy` ([`feature-map.md:308`](../../feature-map.md)) | — | `skill_management__install_local`/`install_source` are threat-scanned and permission-gated before a skill directory is registered — no external skill source reaches the registry without passing the gatekeeper ([`feature-map.md:537`](../../feature-map.md)) | ⊆-parent capability model: a spawned agent's effective capability = parent's live effective ∩ assigned profile, recursively no-escalation-via-spawn, closed across four stale-lineage axes ([`feature-map.md:665`](../../feature-map.md)) | OAuth 2.1 tokens are cached outside the rewind bucket (mode 0600, per-server, never rewound), and a headless run with no cached token fails clearly instead of hanging — external auth material never re-enters a replayed state ([`feature-map.md:522`](../../feature-map.md)) | Tier 2/3 capabilities (`shell` / `mcp` / `file` out-of-zone / `python`) require declaration + 4-layer just-in-time approval (config pre-approval → saved → session → interactive prompt) — no capability reaches the world without passing the gatekeeper ([`feature-map.md:420-426`](../../feature-map.md)) | — |
| **Evaluation** | Scoring an output against a rubric is a pipeline `agent` step + `schema` — the OS's contribution is the typed schema (constrained generation + validation) and the cost tracking it already provides for every `agent` step, not a bespoke scorer op (the prior `judge_output` op was removed as a clean-break: it was an LLM call with a rubric the OS never interpreted, i.e. agent work wearing an OS-op costume) ([`feature-map.md:568`](../../feature-map.md)) | — | — | — | — | — | — |
| **Observability** | Event System (P6): 171 event types, append-only JSONL, `reyn events` replay ([`feature-map.md:242-247`](../../feature-map.md)); `present`'s own `presented` audit event ([`feature-map.md:338`](../../feature-map.md)) | Status chips (Agents / Cost / Model / Tools / MCP / Skills / Hooks / Pipes / Cron) are the chat surface's own live audit-trace facet, each expandable in place — the same operator-visible trace the P6 event log records, surfaced inline rather than replayed after the fact ([`feature-map.md:598`](../../feature-map.md)) | — | `chain_id` propagation traces multi-hop delegation chains in P6 events ([`feature-map.md:650`](../../feature-map.md)) | The MCP resource-subscription push lands as a P6 `mcp_resource_updated` audit-event — the observability trace of the same subscription mechanism whose resync-on-reconnect is the Reliability exemplar above (that push is *also* wired as an external-event hook-point, but that's a reactivity trigger, not an observability facet — the events triad keeps audit-event/hook-event distinct) ([`feature-map.md:519`](../../feature-map.md)) | `limit_denied` is a P6 audit event on every deny path (`max_iterations` / `router_cap`) ([`feature-map.md:443`](../../feature-map.md)) | `reyn events` replays event JSONL files for audit and debug — the CLI-side entry point into the P6 audit trail ([`feature-map.md:370`](../../feature-map.md)) |
| **Product Think** | `present` routes bulk data to the surface at ~0 output tokens instead of reproducing it as LLM output ([`feature-map.md:340`](../../feature-map.md)) | Reyn's chat surface is a local, inspectable CLI with live audit chips (agents / cost / permissions) beside the conversation — the operator sees what the agent is doing and spending in real time ([`feature-map.md:604`](../../feature-map.md)) | Hot-reload: `.reyn/config/skills.yaml` edits apply at the next turn boundary via the `"skills"` reload seam — predictable, config-selectable operator control without a restart ([`feature-map.md:535`](../../feature-map.md)) | Agent hops cap (`safety.loop.max_agent_hops`) gives the operator a predictable, config-selectable bound on delegation depth — legible orchestration, not an unbounded chain ([`feature-map.md:654`](../../feature-map.md)) | Reyn keeps external connectivity to standard protocols (MCP client+server, A2A sync+async, REST/AG-UI SSE gateway) rather than a sprawling per-app integration catalog — a disciplined, legible surface set instead of endless bespoke connectors ([`feature-map.md:582`](../../feature-map.md)) | On-limit modes (`interactive` / `auto_extend` / `unattended`) give the operator predictable, config-selectable control over every loop/timeout/budget checkpoint uniformly ([`feature-map.md:441`](../../feature-map.md)) | High-cost model warn (`cost_warn`): a pre-selection warning to the operator when the resolved model's cost-per-1M-tokens exceeds a threshold, de-duped once per model per session — legibility, distinct from the token/USD *bounding* caps themselves (the cross-cutting band's `cost/budget` member, not a Product Think exemplar) ([`feature-map.md:482`](../../feature-map.md)) |

## Family → feature-map section map

Every one of `feature-map.md`'s 23 live `###` sections falls into exactly one family —
this table is the lossless appendix; keep it in sync if a `###` section is added,
renamed, or removed in `feature-map.md`.

| # | feature-map section | family |
|---|---|---|
| 1 | OS Core | Decision & Tool-Use |
| 2 | Chat Engine | Chat & Session |
| 3 | Control IR Ops | Decision & Tool-Use |
| 4 | Present layer | Decision & Tool-Use |
| 5 | Tool-Use Schemes | Decision & Tool-Use |
| 6 | CLI | Product surface |
| 7 | Config | Safety & Config |
| 8 | Permissions | Safety & Config |
| 9 | Safety / limit-handling | Safety & Config |
| 10 | Content-layer defense | Safety & Config |
| 11 | Budget & Cost | Product surface |
| 12 | Memory & RAG | Context & Retrieval |
| 13 | MCP | External surfaces |
| 14 | Skills | Context & Retrieval |
| 15 | Pipeline | Orchestration |
| 16 | Web & Protocol | External surfaces |
| 17 | Inline CUI | Chat & Session |
| 18 | Intervention | Chat & Session |
| 19 | Sessions and identity | Chat & Session |
| 20 | Multi-Agent | Orchestration |
| 21 | LLM org-design (runtime spawn primitives) | Orchestration |
| 22 | Sandbox | Safety & Config |
| 23 | Environment — ⚗ Stage 2 | External surfaces |

Totals: Chat & Session 4 · Decision & Tool-Use 4 · Context & Retrieval 2 · Orchestration 3 ·
External surfaces 3 · Safety & Config 5 · Product surface 2 = 23/23, each section in
exactly one family. (#2839 Phase 2/3: the internal Task system section was removed —
LLM task-decomposition moves external, MCP + hooks + pipelines.)

## See also

- [`CLAUDE.md`](https://github.com/tya5/reyn/blob/main/CLAUDE.md) — the constitution skeleton (eight lenses' pass-lines + the cross-cutting band) this table populates
- [`docs/feature-map.md`](../../feature-map.md) — the canonical, impl-extracted feature inventory every cell cites
