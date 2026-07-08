# Tool-result schema redesign — proposal

**Author:** lead-coder · **Status:** IMPLEMENTED (all PRs merged 2026-07-08: PR-0 #2647 stale
read-back strings, PR-1 #2648 canonical format + legacy deletion, PR-2 #2652 pipeline ctx, PR-3
#2651 offload config opt-out; follow-ups tracked in #2396 dead-offloader removal + #2649 pipeline
error-format unification). Originally APPROVED (owner GO 2026-07-08, Fable5-reviewed same day, all
review findings incorporated; clean-break ruling: no backward compat, legacy path deleted in-arc) ·
**Date:** 2026-07-08

## Problem

reyn's current LLM-visible tool_result (the `role: tool` chat message content) has three
confirmed defects:

1. **Plumbing leak.** MCP results carry `{kind, status, server, tool, isError, error, ...}`
   alongside the actual content — the LLM only ever wants the result, not which transport
   produced it. `run_pipeline`'s success return leaks `run_id`/`named_stores` alongside the
   one field the caller asked for (`output`). Everything round-trips through `json.dumps`,
   so multi-line text arrives newline-escaped and unreadable.
2. **Whole-dict offload blob.** `decide_payload_field` (`src/reyn/core/context_builder.py`)
   only offloads a field cleanly when it is the **sole** oversized field; when both
   text-shaped and structured-shaped data are large simultaneously, it falls back to
   dumping the whole dict as one unreadable single-line JSON blob. Confirmed root cause #1
   of the owner-observed "the LLM has stopped reading offloaded files" symptom.
3. **Stale read-back instruction.** `read_tool_result` was retired in #1449 (replaced by
   `file__read(path)` — refs are plain files under `.reyn/tool-results/`), but every string
   that tells the LLM how to read an offloaded body back still names the retired tool:
   `tool_result_cap.py:187` (`_offload_note`), `router_loop.py:270`, `router_loop.py:305`.
   An LLM following the instruction calls a nonexistent tool. Confirmed-stale references;
   hypothesis: root cause #2 of the same "stopped reading offloaded files" symptom.

## Consumer enumeration (scoping — who actually sees a tool_result)

1. **Chat turn feedback** (`router_loop.py::feedback()`) — the LLM-visible `role: tool`
   message for a normal (or sub-agent, since `agent:` pipeline steps spawn a session that
   runs the same router loop) chat turn. **In scope.**
2. **CodeAct's own observation formatter** (`schemes/codeact.py::_format_codeact_observation`)
   — an independent `json.dumps` of a tool's raw result into a `[codeact result]` user
   message. Same leak, structurally separate path. **Out of scope for this arc** — tracked
   as a follow-up issue applying the same canonical mapping there.
3. **Durable `tool_returned` event** (audit trail) — untouched; this redesign only changes
   what the LLM *sees*, not what's durably logged.
4. **Pipeline `tool:` step** (`executor.py::_run_tool_step`) and **`agent:` step prompt
   interpolation** (`executor.py::_interpolate_prompt`) — **pipeline internals never
   offload, by owner ruling, not a gap**: a pipeline's author is expected to size their own
   data flow. The ctx *shape* changes (§ Design 3) but no size gate is ever applied there.
   - **Nuance:** an `agent:` step's own LLM, when it autonomously calls tools *during its
     own turn* (not via a `tool:` step), is a normal chat-turn LLM and goes through
     consumer (1) — same offload treatment as regular chat. The "no offload in pipelines"
     rule covers the pipeline's own ctx/pipe data flow only.

## Design principles

- **Clean break (owner ruling 2026-07-08):** no backward compatibility, no transition
  shims. The legacy field-guessing offload path (`decide_payload_field`,
  `_oversized_fields`, the per-op `_offload_payload_field` markers, the sole-oversized
  rule, the whole-dict JSON-envelope fallback) is **deleted in this arc**, not kept
  alongside the new path. This completes the endgame `canonical.py`'s own module docstring
  already declares ("`decide_payload_field`, `_oversized_fields`, the sole-oversized
  condition, and the six per-op markers then disappear").
- **Format ⊥ offload:** the LLM-visible frontmatter format applies **always** — including
  when `media_store` is absent and when offload is config-disabled. Offload/truncation is
  an independent, gateable size concern layered on top. (Today `router_loop.py:2970`
  couples canonicalization to `media_store is not None`; that coupling goes away.
  Without this, the offload opt-out would also flip the format back to the JSON envelope,
  confounding the exact A/B experiment the opt-out exists for.)
- **High signal only** (per MCP SEP-1624 + Anthropic tool-writing guidance): mappers strip
  transport identifiers (`kind`, duplicate `status`, `server`, `tool` echo) and keep only
  fields that change what the LLM does next (e.g. a nonzero `returncode`).

## Design

### 1. Canonical mapping for ALL op kinds (案B endgame, not a partial migration)

Every op kind that today declares `_offload_payload_field` gets a canonical mapper in
`canonical.py::_MAPPERS`, joining the existing three MCP mappers:

| op kind | `text` | `structured` |
|---|---|---|
| `mcp` / `mcp_read_resource` / `mcp_get_prompt` | (existing mappers; meta tightened) | structuredContent / blobs |
| web fetch (`_offload_payload_field: "content"`) | page content | — (url etc. only if signal) |
| web search (`"results"`) | — or rendered summary | results list |
| sandboxed exec (`"stdout"`) | stdout (+stderr) | returncode when nonzero |
| recall / index_query (`"chunks"`) | — | chunks |
| `run_pipeline` (sync) | output if str | output if non-str |
| `run_pipeline_async` | "started" notice **keeping `run_id`** | — |

- **Sync `run_pipeline` drops `run_id` and `named_stores`** from the LLM-visible side
  (owner ruling: the final output is the only thing the calling LLM wants).
- **Async `run_pipeline_async` KEEPS `run_id`** — it is the correlation handle for the
  later `[pipeline]` completion message; e.g.
  `text = "Pipeline started (run_id: <id>). Result will arrive as a [pipeline] message."`
- **Unregistered-kind fallback:** whole result dict → `structured`, `text` empty (renders
  as pure frontmatter YAML — readable, and `ctx.<name>.structured.<field>` still gives
  programmatic access). Replaces the current fallback (whole-dict `json.dumps` into
  `text`).
- Exact per-op signal-field selection (what survives into frontmatter) is the mapper
  author's judgment under the high-signal-only principle; the table above is the guide.

**Deleted in the same arc:** `decide_payload_field`, `_oversized_fields`, all six
`_offload_payload_field` markers (`web.py:504`, `web.py:538`, `recall.py:127`,
`sandboxed_exec.py:138`, `mcp.py:164`, `index_query.py:105`), the sole-oversized decision
in `feedback()`'s non-MCP branch, and `canonical.py`'s whole-dict-into-text fallback.
Tests pinning the old JSON envelope are updated or deleted in the same PR.

### 2. LLM-visible format: frontmatter + text, no envelope

- **Success, no structured/signal-meta** → the plain `text` string. No JSON, no wrapper.
- **Structured data or signal meta present** → frontmatter block, then the text body:
  ```
  ---
  <yaml-serialized structured data + signal meta fields>
  ---
  <text>
  ```
- **Error** → a plain string, never JSON: `Error (<kind>): <message>` — `kind` retained
  from the dispatch envelope's `error.kind` because it is actionable (`permission_denied`
  vs `not_found` imply different recovery actions). MCP `isError: true` results map to
  `Error: <joined content text>` (MCP carries the error description in `content`).
  Success and error are syntactically distinguishable with no status field.
- **Edge guards (implementer notes):** (a) when there is no frontmatter and `text` itself
  starts with `---`, prepend a blank line (or emit an empty frontmatter block) so the LLM
  cannot misparse the body as frontmatter; (b) the existing `_post_text` mechanism appends
  `\n\n---\n<post_text>` — acceptable (frontmatter is at top, post_text at bottom), but the
  implementer may switch its separator to avoid the visual collision.
- YAML serialization: `default_flow_style=False`, `allow_unicode=True` (readable,
  token-efficient vs JSON).

### 3. Pipeline `ctx` exposes the same fields, flat, for ALL tool kinds

`ctx.<name>.text` / `ctx.<name>.structured` (key absent when no structured data) for every
`tool:` step's result — uniform because §1 maps *all* op kinds. `_run_tool_step` runs the
result through `to_canonical` (shape only — **no size gates, no offload, ever**; owner
ruling). Chat-side and pipeline-side thus expose the same two-field schema, so an LLM
authoring a pipeline never juggles two result shapes.

**Breaking change** for any existing pipeline reading raw result fields (e.g.
`ctx.x.data.content`) — clean break: bundled/example pipelines and
`docs/reference/runtime/pipeline-dsl.md` (+ `.ja`, + `write-a-pipeline.md`) are updated in
the same PR. No compat shim.

### 4. Independent offload streams + plain-text previews (bug fixes #2 and #3)

`text` and `structured` are two fully independent offload candidates:

- **text** — capped by `cap_tool_result_content` (token budget, mechanics unchanged), but
  the preview becomes **plain text**, not a JSON stub:
  ```
  <head>
  ...[truncated: <N> chars total — full body: file__read("<ref>")]...
  <tail>
  ```
- **structured** — gated by `STRUCTURED_INLINE_MAX_CHARS` (`seam.py:30`, its own file when
  oversized); the frontmatter then carries the ref + a short preview instead of the data:
  ```
  ---
  structured: offloaded
  structured_ref: <path>       # read back via file__read
  structured_preview: |
    <first ~600 chars>
  ---
  <text>
  ```
- Two large fields now produce **two clean offload files**; the whole-dict single-line
  blob fallback no longer exists (the code that produced it is deleted, §1).
- **Every read-back instruction says `file__read(...)`** — fixing the three stale
  `read_tool_result` strings (Problem #3). This includes `tool_result_cap.py`'s
  `_offload_note` and the two `router_loop.py` media/offload notices.
- Media blocks: unchanged — always extracted and delivered as the existing multimodal
  follow-up `role: user` message, never embedded in the tool_result string.

### 5. Config opt-out — new `offload:` section (debug lever)

**Purpose (owner-stated):** the offload mechanism itself is suspected of degrading LLM
autonomy (over-truncation starving the model of context). The opt-out isolates that
experimentally: offload OFF vs ON, format identical (§ principles), the only variable is
truncation. Debug/experiment lever, not a recommended steady-state setting.

- New `reyn.yaml` section:
  ```yaml
  offload:
    enabled: true   # default; false = never truncate, always emit everything in full
  ```
  Positive boolean in its own section (not a negative `disable_*` flag buried elsewhere);
  the section is also the future home for related knobs (e.g. making
  `STRUCTURED_INLINE_MAX_CHARS` operator-tunable — currently an uncustomizable hardcode).
- When `enabled: false`, **all three size gates** are disabled:
  1. the text token cap (`cap_tool_result_content`),
  2. the structured inline gate (`STRUCTURED_INLINE_MAX_CHARS` in `build_offload_body`),
  3. the media follow-up budget bound (`media_followup_budget`) — included so the
     experiment isn't confounded by media starvation.
- **Mechanism (corrected by review):** early-return in
  `ContextBudgetAdvisor.cap_tool_result()` + a flag threaded to `build_offload_body` and
  the media-budget call site. **NOT** by forcing `per_turn_cap_tokens()` to 0 — that value
  also feeds `media_followup_budget = max(0, per_turn_cap_tokens() - text_tokens)`
  (`context_budget_advisor.py:137`), so zeroing it would silently kill all media
  follow-ups. `per_turn_cap_tokens()` semantics stay untouched.
- **Known risk (documented, accepted):** with offload disabled, a single tool result can
  exceed `B_M`, recreating compaction dead-end #1 (a turn too large to ever compact —
  exactly what #1128 closed). Emit a warning event/log when a session starts with
  `offload.enabled: false` so traces are self-explaining.

## Quick-win PR (independent, dispatch first)

The three stale `read_tool_result` → `file__read` strings (Problem #3) are a tiny,
self-contained fix shippable immediately, independent of everything else in this doc, and
plausibly deliver most of the "LLM reads offloaded files again" recovery on their own.
Dispatch as PR-0 before the main arc.

## Suggested PR sequencing

- **PR-0** — stale read-back strings → `file__read` (quick win, ships today).
- **PR-1** — canonical mappers for all remaining op kinds + frontmatter format +
  `feedback()` generalization (trigger drops the kind-list, canonicalizes everything) +
  plain-text previews + **deletion of the legacy path** (markers, `decide_payload_field`,
  `_oversized_fields`, envelope format, old fallback) + test updates.
- **PR-2** — pipeline `ctx.text/.structured` exposure + pipeline docs updates.
- **PR-3** — `offload:` config section + 3-gate opt-out + warning event.

(Sequencing is guidance; PR-1 is the large one and may split mappers/format if the coder
prefers — but the legacy-path deletion must land with the format switch, never as a
follow-up.)

## Test plan (per testing.ja.md tiers)

- **Tier 1 (contract):** per-op mapper contracts (text/structured split, signal-meta
  selection, error-string format incl. `Error (<kind>):` and MCP `isError` mapping);
  async `run_pipeline_async` result retains `run_id`, sync drops it.
- **Falsify test (bug #2):** construct a result with BOTH oversized text and oversized
  structured → assert two clean offload files and that no single-line whole-dict blob is
  emitted. Must be RED against the pre-change code path's behavior (write against the new
  seam, verify the old fallback is gone).
- **Config round-trip (non-default value):** `offload.enabled: false` in a real config
  file reaches all three gates (per feedback_roundtrip_test_nondefault_value).
- **Live-path test:** real router `feedback()` path emits frontmatter format (mirror of the
  #2631 live-path SP test pattern) — including with `media_store=None` (format must not
  depend on store presence).
- **No Tier-4 pins:** no exact-whitespace/YAML-ordering assertions on the frontmatter;
  assert field presence/absence and substrings.
- Existing tests pinning the JSON envelope: updated or deleted in PR-1.

## Explicitly out of scope

- CodeAct's `_format_codeact_observation` — same principles, separate follow-up issue
  (file it when PR-1 lands, referencing this doc).
- `_interpolate_prompt` size-capping — not a gap; pipelines are deliberately offload-free
  per owner ruling.
- Any change to the durable `tool_returned` audit event shape.

## Owner rulings log

- 2026-07-08: design GO ("それでオッケーです。実装進んでください"), then paused for Fable5 review.
- 2026-07-08 (Fable5 review reflected): clean break confirmed — "後方互換性のような技術負債は
  無くすべき"; full-op mapper migration + legacy-path deletion adopted; all review findings
  (media-budget-safe opt-out seam, structured-gate inclusion, format⊥offload decoupling,
  plain-text previews, `Error (<kind>)`, async `run_id` retention, `offload:` config
  section, stale `read_tool_result` fix as PR-0) incorporated.
- Pipeline internals never offload; sync pipeline result = output only (earlier rulings).
