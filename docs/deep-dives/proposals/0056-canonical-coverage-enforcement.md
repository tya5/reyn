# Canonical-mapping coverage enforcement — no tool result without a declared LLM-visible shape

**Author:** architect · **Status:** ✅ IMPLEMENTED 2026-07-09 — all three code PRs
merged: **PR-H #2677** (hotfix mappers) · **PR-F1 #2680** (registration-seam declaration +
identity dispatch + registry-derived gate + `CANONICAL_TODO` grandfather ratchet) ·
**PR-F2 #2684** (`canonical_fallback_used` visibility event, 6ccc70ed). Owner-reviewed +
lead-coder APPROVE. As-built deltas verified against merged code — see
[As-implemented](#as-implemented-verified-against-merged-code-2026-07-09) · **Date:** 2026-07-09 ·
**Builds on:**
[FP-0053 tool-result schema redesign](0053-tool-result-schema-redesign.md) (IMPLEMENTED)

## Incident (dogfood, 2026-07-09)

Reading `docs/reference/runtime/present.ja.md` via `reyn_source__read` in the `user`
dogfood checkout confused the agent: the result was offloaded as a **whole-dict
`structured` attachment** (`{"path": ..., "content": ...}` in
`.reyn/tool-results/*--structured-1.txt`), so the LLM saw `structured: offloaded` + a
600-char preview of a JSON dict instead of a readable text body. Six such offloads in
three minutes as the user retried.

### Diagnosis (primary data)

1. `to_canonical` (`core/offload/canonical.py`) dispatches on `result["kind"]`;
   `_MAPPERS` registers **10 kinds** (mcp×3, web×2, sandboxed_exec, recall,
   index_query, run_pipeline×2). Everything else takes the documented fallback:
   whole dict → `structured` attachment, `text` empty — **silently**.
2. The `reyn_src_read` handler (`tools/reyn_src.py`) returns `{path, content, ...}`
   with **no `kind` field at all** → `kind=None` → fallback. The `file` op *does* set
   `kind: "file"` but has **no mapper either** → same fallback for every
   read/grep/glob/write.
3. **Not a regression — an inherited gap.** The pre-arc offload blob
   (2026-07-07, `*--tool-1.txt`) is the whole `{"status":"ok","data":{...}}` envelope
   as JSON: file-family reads were *never* cleanly offloaded. FP-0053's mapper table
   scoped migration to the ops that declared `_offload_payload_field` (six markers) +
   mcp/pipeline — `file`/`reyn_src` never had a marker, so **the ratified design
   itself omitted the most common read path**, and the implementation followed the
   design faithfully. Hand-enumeration was the single point of failure.

### Coverage audit (kinds that can reach the feedback chokepoint)

| Source | Canonical mapping | Effect today |
|---|---|---|
| `file` op (read/grep/glob/write/edit/delete) | ❌ none (`kind:"file"` unmapped) | whole-dict structured blob |
| `reyn_src_read/list/glob/grep` handlers | ❌ none (**no `kind` in result**) | whole-dict structured blob |
| `compact`, `judge_output` | ❌ none | blob if oversized |
| `mcp_install`, `mcp_drop_server`, `mcp_(un)subscribe_resource`, `skill_install`, `pipeline_install` | ❌ none | blob (small results; latent) |
| mcp×3, web×2, sandboxed_exec, recall, index_query, run_pipeline×2 | ✅ mapped | correct |

## The structural defects (why patching one mapper is not the fix)

1. **Two hand-maintained registries with no forced correspondence.** Op/tool
   registration (`OP_KIND_MODEL_MAP`, ToolDefinition registries) and `_MAPPERS` are
   separate dicts in separate files; nothing makes "registered an op/tool" imply
   "declared its LLM-visible shape". The repo already rejects this pattern elsewhere
   (#1983 keeps `OP_KIND_MODEL_MAP` ↔ control-ir.md in sync; ToolDefinitions derive
   from IROp models "so the catalog never drifts").
2. **Dispatch sniffs `result["kind"]` — data the producer may not even provide.** The
   feedback chokepoint *knows what it invoked* (tool name / op kind), yet
   canonicalization guesses from the result dict. Any handler that forgets to
   self-describe silently falls through — unfixable by adding mappers alone.
3. **The fallback is fail-open and silent.** Exactly the class the team pinned in
   #2670 (`get_neutralizer` silent fail-open) and that FP-0055's sink contract forbids
   (unknown lookup must fail closed / visibly). This gap shipped through design review,
   implementation, and CI, and was found only by a human noticing the agent "being
   confused" in dogfood.

## Design — three reinforcing layers

**Contract:** *every* LLM-visible tool-result producer has an **explicitly declared**
canonical mapping; "undeclared" is a CI failure, not a runtime fallback.

### 1. Canonical declaration at the registration seam (co-location)

- Registering an LLM-invocable producer — an op kind in the op-runtime registry, or a
  router ToolDefinition (e.g. `reyn_src_read`) — **requires** a `canonical` declaration:
  either a mapper function, or the explicit named opt-in
  `canonical=STRUCTURED_PASSTHROUGH` (a deliberate, greppable, reviewable choice for
  admin-ish ops whose whole dict *is* the right LLM view). Accidental omission becomes
  impossible at the same place the tool is born — the mapper is part of the op/tool
  contract, like its schema (single-source + derive, the house pattern).
- **Dispatch by invoked identity, not result sniffing:** the feedback chokepoint
  resolves the mapper from *what was called* (`to_canonical(result, source=<tool/op
  id>)`). `result["kind"]` stops being load-bearing for canonicalization (it remains
  ordinary result data). This fixes the kind-less-handler class outright.

### 2. Completeness gate (CI, Tier 1 — registry-derived, not hand-listed)

- A parametrized test walks **every registered op kind and ToolDefinition** and asserts
  an explicit canonical declaration exists; new producer without one = red CI naming
  it. Mapper-output invariants asserted alongside (text is `str`, structured is
  JSON-serializable, no dispatch-envelope leakage).
- Key property: the gate enumerates from the **registries**, not from a doc table —
  FP-0053's table was hand-written and missed `file`; a registry-derived gate catches
  even *design-level* omissions.

### 2a. `CANONICAL_TODO` marker + grandfather ratchet (a third, self-liquidating declaration category)

Migrating to the gate surfaced ~40 non-admin producers (memory/cron/catalog/task/
agent-spawn/hooks/mcp-discovery/universal-wrappers, etc.) with no real mapper. Writing
40 real mappers is out of PR-F1 scope **and not behavior-preserving** (whole-dict→text
changes what the LLM sees), so they need a way to pass the gate now. The wrong way is to
mark them `STRUCTURED_PASSTHROUGH`: that conflates *"whole dict is legitimately the LLM
view"* (the admin/install decision-#1 semantics) with *"no mapper yet"* — and would
**bless a latent `file`-class bug** (a text-content producer whose whole-dict output is
really text) as an intentional passthrough, defeating the very gate that was meant to be
load-bearing.

So a **distinct** marker `CANONICAL_TODO` carries the "declared-to-pass-the-gate, not
yet mapped" meaning, kept semantically separate from `STRUCTURED_PASSTHROUGH`. On its own
a third green declaration would hollow out the gate — any new producer could reach for
`CANONICAL_TODO` and the anti-regression property evaporates. The **grandfather ratchet**
is what keeps the gate load-bearing:

- **Grandfather-only membership.** `CANONICAL_TODO` is usable **only** by the explicit
  `frozenset _CANONICAL_TODO_GRANDFATHERED` — the source-ids relabeled in this migration,
  enumerated in code. A producer outside the set that declares `CANONICAL_TODO` = **red
  CI**. New op-kinds / ToolDefinitions may declare only a real mapper or
  `STRUCTURED_PASSTHROUGH` — never `CANONICAL_TODO`.
- **Monotonic burn-down.** The allowlist only shrinks: as each grandfathered producer
  gets a real mapper it leaves the set, and nothing may ever be added. One frozenset thus
  enforces both *count-non-increasing* and *new-producer-ban* at once.
- **Tracked, greppable.** Each `CANONICAL_TODO` declaration references the burn-down
  tracking issue (#2681), so the remaining debt is one grep and has an owner.

**Hot-text triage precedes deferral (no rubber-stamped inference).** Before parking all
40 under the marker, each producer's result shape is **directly inspected** and classified
text-shaped / structured / status. Only **text-shaped** producers (the latent `file`-class
risk) get a real mapper inside F1; genuinely structured/status producers are the ones
deferred under `CANONICAL_TODO`. The "40 hide a `file`-class producer" claim is a
**hypothesis to confirm by inspection**, not a fact to extrapolate from 1–2 samples — the
triage classification is listed in the PR as #2681's starting material.

### 3. Runtime fallback kept for true unknowns — but visible

- Genuinely unregistered sources (dynamic/edge cases the registries cannot enumerate)
  keep the lossless whole-dict fallback — but it emits a **`canonical_fallback_used`
  P6 event** (+ warn log) naming the source. Degrade-with-audit, never silently: this
  incident would have been one trace-grep instead of a human noticing confusion.

## Immediate hotfix (PR-H — ships first, independent of the framework)

The live dogfood pain must not wait for the refactor:

- `file` mapper: `read` → `content` as `text` (path/op/status as signal meta,
  high-signal rule); `grep`/`glob` → rendered match/path lines as `text`;
  `write`/`edit`/`delete` → short status text.
- `reyn_src_*` handlers: route through the same mapping (set `kind:"file"` or map the
  tool names — implementer's call; under the framework this collapses into the
  registration declaration anyway).
- `compact` / `judge_output` mappers.
- **Regression test = the incident**: doc-file read → offload → text-stream offload
  file whose body is the readable document + `file__read` note; falsify that no
  whole-dict structured attachment is produced for a file read.

## Sequencing

- **PR-H** — hotfix mappers + incident regression test (small; unblocks dogfood).
- **PR-F1** — registration-seam `canonical` declaration + identity-keyed dispatch +
  registry-derived completeness gate (medium; deletes `_MAPPERS` as a free-floating
  dict).
- **PR-F2** — `canonical_fallback_used` event + fallback visibility (small; can fold
  into F1).

## Test plan (per testing.ja.md tiers)

- **Tier 1:** per-mapper contracts for the hotfix (file read/grep/glob text shaping,
  signal-meta selection); completeness gate over all registered producers;
  `STRUCTURED_PASSTHROUGH` opt-ins are exactly the reviewed list; identity-keyed
  dispatch resolves for a kind-less result; mapper-output invariants.
- **Tier 2:** fallback path emits `canonical_fallback_used` with the source id; no
  content bytes in the event.
- **No Tier-4 pins:** assert presence/absence and substrings, not YAML ordering.

## Relation to standing principles

- #1983 / single-source-derive: the mapper joins the schema as part of one
  registration contract instead of a parallel hand-synced registry.
- #2670 / FP-0055 sink contract: unknown lookup fail-closed (at CI) or fail-visible
  (at runtime) — never silent fail-open.
- Pinned team feedback "ratified design is the drift tiebreaker": here the ratified
  design itself had the omission — registry-derived gates are the mechanical answer to
  hand-written enumeration in design docs.

## Decisions (owner deferred to architect recommendation, 2026-07-09)

1. **`STRUCTURED_PASSTHROUGH` initial membership** = the admin/install family only:
   `mcp_install`, `mcp_drop_server`, `skill_install`, `pipeline_install`,
   `mcp_subscribe_resource`, `mcp_unsubscribe_resource`. Every other producer gets a
   real mapper — passthrough is the reviewed exception, not the default.
2. **PR-F2's `canonical_fallback_used` event also fires when a declared
   `STRUCTURED_PASSTHROUGH` result exceeds the structured offload gate** — a passthrough
   op producing an oversized structured blob is a signal that passthrough was the wrong
   choice for it; make it visible rather than silent, same audit-not-silence principle.
3. **`CANONICAL_TODO` is a grandfather-only, monotonic-burn-down, new-producer-banned
   marker** (see §2a), kept distinct from `STRUCTURED_PASSTHROUGH` so the migration debt
   never conflates with the reviewed admin/install passthrough set and never becomes a
   gate escape hatch. Membership lives in one `frozenset` that only shrinks; each entry
   references the #2681 burn-down issue. Text-shaped producers are mapped in F1 (not
   deferred) after direct shape inspection. *(Ratified 2026-07-09: architect ratchet
   refinement + lead-coder concurrence; delivered to the F1 coder before the marker was
   finalized.)*

## As-implemented (verified against merged code, 2026-07-09)

Stamped after reading the merged sources/tests directly (not a rubber-stamp — the
counts below correct the approximate figures used during dispatch).

1. **Ratchet (§2a) landed as an EQUALITY assert.**
   `tests/test_fp0056_canonical_coverage_gate.py` holds
   `_CANONICAL_TODO_GRANDFATHERED = frozenset({…})` (**52 entries**) and asserts
   `live_todo == set(_CANONICAL_TODO_GRANDFATHERED)` — so *adding* a producer to the
   marker (new debt) OR *forgetting to remove* one after mapping (stale) both go red.
   Equality — not subset — is what enforces new-producer-ban **and** monotonic burn-down
   in one set. `_STRUCTURED_PASSTHROUGH_ADMIN_6` (**6 entries**: `mcp_install`,
   `mcp_drop_server`, `skill_install`, `pipeline_install`, `mcp_subscribe_resource`,
   `mcp_unsubscribe_resource`) is asserted equal to the live passthrough set — Decision #1
   honored exactly.
2. **Hot-text triage — hypothesis tested, mostly negative.** The migration surfaced
   **~54 provisional producers**; direct shape-inspection classified exactly **2 as
   text-shaped** (`read_memory_body` → the memory body text; `ask_user` → the user's
   answer), which got **real mappers in F1** (`core/offload/canonical.py:545,566`). The
   remaining **52 are genuinely structured/status-only** and sit under `CANONICAL_TODO`
   as the #2681 burn-down ledger. The feared "the 40+ hide a `file`-class text producer"
   risk was confirmed by inspection to be small (2 of ~54) rather than extrapolated —
   the "confirm by inspect, don't rubber-stamp inference" instruction held.
3. **PR-F2 `canonical_fallback_used` — three fire conditions, content-free.**
   `canonical_fallback_reason(source, *, structured_offloaded)`
   (`canonical.py:611-640`) returns one of three category strings, else `None`:
   - `"unregistered"` — genuine unknown the registries can't enumerate.
   - `"canonical_todo"` — a grandfathered marker producer took the whole-dict fallback
     (the #2681 debt made runtime-visible; feeds burn-down prioritization).
   - `"passthrough_oversized"` — a `STRUCTURED_PASSTHROUGH` result whose whole-dict
     serialization exceeded the structured offload gate (caller passes
     `structured_offloaded=True`; Decision #2). A **small/inline** passthrough is a
     reviewed legitimate view → `None` (no event). A **real mapper always returns
     `None`** — a mapped producer never took a fallback.
   Only the category + `source` id are emitted; **no result body** is ever returned or
   logged (audit signal, not data).

## References

- FP-0053 (IMPLEMENTED) — mapper table scoped to `_offload_payload_field` holders;
  fallback semantics; `router_loop.py` #2425 chokepoint.
- Incident data: `~/Workspace/reyn_dev/user/.reyn/tool-results/2026070{7,8}T*` (pre-
  and post-arc whole-dict blobs), events `2026-07-09T0739*.jsonl`.
- `core/offload/canonical.py` (`_MAPPERS`, `to_canonical` fallback), `tools/reyn_src.py`
  (kind-less results), `core/op_runtime/file.py` (`kind:"file"`, unmapped).
- #2670 (fail-open lookup — same class), FP-0055 §structural sink-neutralization
  contract (fail-closed principle).
