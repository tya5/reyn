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
- Authoring proceeds family-by-family (one PR per column). Not-yet-authored
  columns are marked **"*(pending)*"**, distinct from a deliberately-empty "—" cell
  within an authored column.

## The 8×7 grid

| Lens | Decision & Tool-Use | Chat & Session | Context & Retrieval | Orchestration | External surfaces | Safety & Config | Product surface |
|---|---|---|---|---|---|---|---|
| **System Design** | The agent loop is an OS-enforced contract: every side effect is a schema-validated, typed Control IR op, never a free-form string ([`feature-map.md:249`](../../feature-map.md)) | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* |
| **Tool Contract** | Op kinds mirror `OP_KIND_MODEL_MAP` 1:1 in `schemas/models.py`, one typed schema per kind ([`feature-map.md:301`](../../feature-map.md)); every tool-use scheme dispatches through the same exclude → permission → dispatch gate regardless of presentation ([`feature-map.md:357`](../../feature-map.md)) | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* |
| **Retrieval** | `recall`: embed query → `index_query` per source → merge top-K ([`feature-map.md:318`](../../feature-map.md)) — context-retrieval, not to be confused with the `retrieval` tool-use *scheme* ([`feature-map.md:354`](../../feature-map.md)), which retrieves tools, not context | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* |
| **Reliability** | Crash Recovery: `.reyn/` recovery-core classification, WAL state log, forward-replay resume, `CommittedStep` memo ([`feature-map.md:207-217`](../../feature-map.md)) | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* |
| **Security** | `present`'s `data_ref` read authority resolves identically to `file.read` ([`feature-map.md:333`](../../feature-map.md)); `sandboxed_exec` runs under a declared `SandboxPolicy` ([`feature-map.md:308`](../../feature-map.md)) | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* |
| **Evaluation** | `judge_output`: LLM scorer with rubric + threshold + `on_fail` policy ([`feature-map.md:321`](../../feature-map.md)) | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* |
| **Observability** | Event System (P6): 171 event types, append-only JSONL, `reyn events` replay ([`feature-map.md:242-247`](../../feature-map.md)); `present`'s own `presented` audit event ([`feature-map.md:338`](../../feature-map.md)) | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* |
| **Product Think** | `present` routes bulk data to the surface at ~0 output tokens instead of reproducing it as LLM output ([`feature-map.md:340`](../../feature-map.md)) | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* | *(pending)* |

## Family → feature-map section map

Every one of `feature-map.md`'s 24 live `###` sections falls into exactly one family —
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
| 22 | Task system | Orchestration |
| 23 | Sandbox | Safety & Config |
| 24 | Environment — ⚗ Stage 2 | External surfaces |

Totals: Chat & Session 4 · Decision & Tool-Use 4 · Context & Retrieval 2 · Orchestration 4 ·
External surfaces 3 · Safety & Config 5 · Product surface 2 = 24/24, each section in
exactly one family.

## See also

- [`CLAUDE.md`](https://github.com/tya5/reyn/blob/main/CLAUDE.md) — the constitution skeleton (eight lenses' pass-lines + the cross-cutting band) this table populates
- [`docs/feature-map.md`](../../feature-map.md) — the canonical, impl-extracted feature inventory every cell cites
