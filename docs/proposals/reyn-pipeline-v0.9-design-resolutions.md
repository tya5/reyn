# Reyn Pipeline v0.9 — Design Resolutions

Concrete resolutions for the load-bearing design decisions identified in `reyn-pipeline-improvements-proposal.md` (S-series + N-series). These are the *design content* of the v0.9 spec pass; the full prose re-issue of the 867-line spec (folding these + the reconciliation corrections) is a mechanical follow-up. An implementer builds against `reyn-pipeline-spec-v0.8.md` + this file until v0.9 is prosed. **Uncommitted working note.** Lead-authored 2026-07-04.

Resolved here: **N3** (expression grammar — first, everything depends on it), **N1** (nested schema/types), **N2** (pipe-data threading), **S1** (recovery model). S2/S3/S4 fold in as noted.

---

## R1 — Expression language grammar (resolves N3; confirms S2)

The language is a **total combinator expression language** — terminating by construction, statically analyzable (data-flow enumerable), and expressive enough for real glue. It is **NOT** restricted Python and **NOT** the CodeAct safe-AST (that gate is for `agent`-invoked Turing-complete Python — a different trust context; conflating them destroys totality + static analysis).

### Grammar (precise)
```
Expr    = Literal | Path | Unary | Binary | Object | List | Combinator
Literal = bool | number | string | null
Path    = IDENT ("." IDENT)*            # ctx.review.passed, item.file  (dotted, static)
Unary   = "not" Expr | "-" Expr
Binary  = Expr ("==" | "!=" | "<" | ">" | "<=" | ">=" | "and" | "or"
                | "+" | "-" | "*" | "/") Expr
Object  = "{" (IDENT ":" Expr ("," IDENT ":" Expr)*)? "}"     # {passed: ..., items: results}
List    = "[" (Expr ("," Expr)*)? "]"
Lambda  = IDENT "->" Expr               # ONLY as a combinator argument; never stored/first-class
Combinator =
    map(Expr, Lambda)      # project/transform each element
  | filter(Expr, Lambda)   # keep elements where Lambda is true
  | all(Expr, Lambda)      # ∀
  | any(Expr, Lambda)      # ∃
  | count(Expr)            # length of a list
  | sum(Expr)              # numeric sum of a list
  | find(Expr, Lambda)     # first matching element (or null)
  | join(Expr, string)     # join a list of strings with a separator
  | get(Expr, Path-literal, default?)   # safe nested access with default
```

### Rules that keep it TOTAL + statically analyzable
- **No recursion, no user-defined functions, no unbounded loops.** Every combinator iterates over a finite list exactly once. `Lambda` is *only* the inline argument to a combinator — it cannot be named, stored, or passed around (defunctionalization, consistent with appendix C line 861). So all programs terminate.
- **No calls to tools / pipelines / LLM / shell / IO.** Pure over the Context. (Hard rule 1 preserved.)
- **Static data-flow**: because `Path` is dotted-static and combinators are structural, the analyzer can enumerate every `{ctx.*}` an expression reads and every field it produces — feeding §7.3's dataflow graph. `map`/`filter`/`get` navigate typed structure (see R2).

### Notes
- This resolves the internal inconsistency where the examples (`all(results, r -> r.verified)`, `{glossary: {}, summaries: []}`, `join(review.comments, "\n")`) exceeded line 761's stated grammar. The grammar above admits exactly those forms and pins the rest.
- `get(expr, path, default)` is added specifically to make free-form / optional-field access safe (addresses N5 — expression referencing an absent field returns the default instead of failing, when the author opts in; a bare `Path` to an absent field is a **step failure**, statically warned if the source has no schema).
- **Calibration commitment (S2/N3)**: the combinator set above is the *v0.9 baseline*; validate it against appendix A's 10 tasks + appendix C's reshape needs (map/filter/pluck/group/rename/merge) during the evaluator build. If a genuine reshape need can't be expressed (e.g. group-by), add exactly one combinator (`group_by(expr, Lambda)`), never a general escape hatch.

---

## R2 — Schema / type system with nesting (resolves N1)

Extend §5.6 so the schema language can express what `match.on`/`until`/`carry_forward`/`over` actually reference. Monomorphic (appendix C's "no parametric polymorphism" stands — this is nesting, not generics).

### Field types
```
Schema = name:NAME fields:{ FIELD: FieldType }+
FieldType = { type: Scalar, required? }
          | { type: "enum", values:[LABEL+], required? }
          | { type: "list", of: ElemType, required? }        # typed list — `of` REQUIRED
          | { type: "object", fields:{FIELD: FieldType}+, required? }   # inline nested object
          | { type: "ref", schema: REF, required? }           # reference another schema
Scalar   = "bool" | "string" | "number"
ElemType = Scalar | {type:"enum",...} | {type:"ref",schema:REF} | {type:"object",fields:...}
```

### What this unlocks
- Real agent outputs: `{approved: {type: bool}, feedbacks: {type: list, of: {type: ref, schema: feedback}}}` where `feedback = {file:{type:string}, comment:{type:string}}`.
- `match.on: "review.approved"` / `until: "ctx.review.approved"` — nested-path references now **statically validatable** (rule 7 becomes real).
- `for_each over: "ctx.suspects"` where `suspects` is `{type: list, of: {type: ref, schema: file}}` — the analyzer knows each `{item}` is a `file`, so `do`'s `{item.path}` references are checked (resolves N8's `over` element-type check).
- `carry_forward: ["review.feedbacks"]` — a nested path, now typed.

### Rules
- A `list` field **must** declare `of` (no untyped lists) — this is what makes `over`/`map`/`filter` statically checkable.
- Path validation (rule 7): every path in `match.on`/`until`/`carry_forward`/`transform`/`over` must resolve through the declared (possibly nested) schema of its source. **Omitting a schema** on an output that is later navigated → static **warning** (N5): "navigating unvalidated structure."
- Depth is bounded by the schema definitions themselves (no recursive schemas in v0.9 — a schema may not reference itself transitively; keeps totality of validation).

---

## R3 — Uniform pipe-data / output rule (resolves N2)

**Every step and every primitive produces exactly one return value.** That value is simultaneously:
1. the **pipe data** handed to the next step (§5.1), and
2. written to a named store iff `output:NAME` is declared (§5.2).

Return value per construct:
| Construct | Return value (= pipe data + optional output:) |
|---|---|
| `agent` / `transform` / `tool` / `shell` | the step's own output |
| `for_each` / `parallel` | the `collect` result |
| `fold` | the final accumulator |
| `match` | the chosen target's final output |
| `call` | the callee's final output |

### Spec edits this implies
- `match` gains an **optional `output:NAME`** (like `call`); its example (§2.5) must show that the chosen branch's result is the match's return / pipe data. Currently the §2.5 example has no destination — that's the bug this fixes.
- Rule 5 already says `output:NAME` doubles as the cross-step value; make explicit that it **also is the pipe data**, and that this holds for primitives (their `collect`/final result is the pipe data).
- A step with no `output:` still produces pipe data (feeds the next step) — it just isn't stashed in a named store.

This makes "what is the input to step N+1?" total and unambiguous, which the executor requires.

---

## R4 — Recovery model: step-boundary generation snapshots + exactly-once replay (resolves S1)

The executor is a **session-typed deterministic driver** (already decided). Its recoverable control-plane state per run:
```
{ run_id, pipeline_id, definition_snapshot_ref,
  scope_stack: [ {step_index, refine_iteration, carry_forward_values} ... ],
  named_stores: {NAME: value}, pipe_data: value,
  completed_step_results: {step_path: result} }   # for replay-not-re-execute
```

### Mechanism (reuses the proven config-generation pattern)
- **Definition snapshot at invocation** (N9): the pipeline + its transitive `call` closure is captured at run start; replay is always against this fixed definition. A mid-run hot-reload/edit does not affect an in-flight run.
- **At each step boundary** (after a step completes, before advancing): record a **full-state generation snapshot** of the control-plane state, keyed at the **durable WAL seq** (`state_log.last_durable_seq`), via a `record_pipeline_state(...)` seam modelled on `core/events/config_recovery.py:record_config_generation` — truncation-surviving, stored under `.reyn/pipeline/state/<run_id>/@<seq>`.
- **Side-effecting steps (tool/shell with non-read effects)**: the step's **result is captured in the snapshot recorded AFTER the effect completes but BEFORE advancing**. There is a small unavoidable window (effect done, snapshot not yet durable) — narrow it by recording the snapshot with `append` (await-durable) for side-effecting steps, `append_nowait` acceptable for pure steps.

### Recovery / rewind
- On restart: load the latest snapshot for the run. Steps whose result is in `completed_step_results` are **replayed from the snapshot, not re-executed** — no double side effects. Execution resumes at the first step with no recorded result.
- On **rewind to seq N** (time-travel): materialize the pipeline state from the latest generation ≤ N (same as config generations), so a rewound pipeline resumes from its position at N.
- **Mandatory truncate-falsify test** (CLAUDE.md recovery gate): run to step K → truncate WAL below step-K's snapshot seq → reconstruct → assert resume at K+1 with correct `named_stores`/`pipe_data` and NO re-execution of steps ≤ K.

### Interaction with S4 (loop side-effects)
Retry/refine re-run steps from a boundary; the same "completed-step-result replay" prevents re-running an already-succeeded side-effecting step **within** a retry/refine only if it's outside the re-run scope. For side-effecting steps **inside** a retry/refine scope, S4's structural rule applies (static warning unless `at_least_once_ok`/`idempotent`), because loop semantics intentionally re-execute the scope — recovery-replay does not cover that case.

---

## R5 — Agent-step run+collect (grounded by the run+collect trace 2026-07-04)

An `agent` pipeline step = spawn an ephemeral session, feed the prompt, run it, collect the structured output, close. The seam:
- **Basis = `MessageBus.request`** (`runtime/message_bus.py`) — the existing synchronous run+collect: `await session._put_inbox("user", {text: prompt, chain_id})` → pump `run_one_iteration` on the caller's own task (synchronous — the LLM `await` runs inline) → drain `outbox` into collected `OutboxMessage`s → return at quiescence. NOT the delegation path (`send_to_agent`/`agent_response`) — that's async and requires the caller to itself be a router-loop session.
- **Flow**: `spawn_ephemeral_session` (A2) → `MessageBus.request(session, "user", {text, chain_id})` → join the `kind="agent"` `OutboxMessage.text` → if a `schema` is declared: JSON-parse defensively + `schema.validate(value, schema, registry)` (post-hoc, exactly the `ToolStep` pattern — there is NO schema-constrained generation in the router path). The ephemeral session **self-vanishes** via `_maybe_schedule_ephemeral_vanish` after `run_one_iteration` — no explicit close needed.
- **New thin surface**: `run_agent_step(registry, *, identity, prompt, capabilities, schema=None) -> Any` composing spawn → `MessageBus.request` → parse/validate. (`MessageBus.request` needs a bare convenience form — it currently expects an MCP/A2A-oriented `reply_to`/`TransportRef`.)

### v1 constraints (close the trace's two risks)
1. **Forbid delegation inside `agent` steps.** `MessageBus.request`'s quiescence predicate only checks `inbox.empty()` (not pending chains/tasks), so a mid-turn `send_to_agent` would make it falsely return early. In v1 an `agent` step is a **leaf worker**: its `capabilities` must NOT include `send_to_agent`/delegation (enforce via the narrowing deny-set + a static-analysis check). If delegation-in-agent-steps is wanted later, fix the quiescence predicate first.
2. **Structured output is post-hoc, not constrained.** The agent returns free text; the step JSON-parses + validates against the declared schema; a parse/validate failure is a normal step failure (→ retry/error path). No LLM-native json-mode wiring in v1 (that's a separate `response_format`-through-the-router-turn enhancement).

### Recovery (R4) interaction
An `agent` step's RESULT is recorded in the step-boundary snapshot like any step; on resume a completed agent step is **replayed from the snapshot, NOT re-executed** — the LLM turn (and any tool side effects it made) does not re-run. The existing R4 `completed_step_results` machinery covers this unchanged.

## R6 — Agent → pipeline invocation (owner design 2026-07-04)

**Structural fact**: a session has exactly ONE `ExecutionDriver` (A1). The agentic-loop session runs `RouterLoopDriver`; a pipeline runs the pipeline-executor driver. They cannot coexist or swap in one session → a pipeline ALWAYS runs in a **separate driver-session** the invoking agent spawns (under the agent's identity, ⊆ its capability). Spawning it as a session (not a standalone coroutine) is what gives **crash auto-resume** (`registry.restore_all` restores the driver-session, which resumes the pipeline from its R4 snapshot).

**Two invocation tools** (both spawn a driver-session; both denied to pipeline-internal `agent` steps per S3 cost-bound; nesting inside a pipeline is only `call`):
- `run_pipeline(name, input)` — a REGISTERED, pre-approved pipeline (approval by the transitive-closure hash, §7.1). Surfaced to the agent like the skill L1 list (name + description + derived input interface).
- `run_pipeline_inline(definition, input)` — an AD-HOC pipeline the agent GENERATES at runtime (appendix B is a "compact spec for generation" — this is the DSL's generation-oriented design realized). Constraints:
  - **(a) Static analysis (§7.3) runs before execution** — the safety net for a generated artifact: reject on invalid cost-bound / path-dataflow / malformed structure. This makes the static analyzer the **runtime validation gate for agent-generated pipelines** — reinforcing P4 (build the analyzer alongside the primitives; it is not optional polish).
  - **(b) Capability ⊆ the invoking agent** (narrow-only) → no privilege escalation is structurally possible, so an inline pipeline needs no separate pre-approval beyond the agent already holding the `run_pipeline_inline` permission; sensitive individual ops still hit the existing per-tool permission gate.
  - **(c) Denied to pipeline-internal `agent` steps** (S3 — same cost-bound protection as `run_pipeline`).

**Session hierarchy constraints** (spawn tree: invoking-agent A → pipeline-driver-session D → per-agent-step ephemeral sessions E_i):
1. **Capability narrows transitively**: `E_i ⊆ D ⊆ A` (§3.4 narrow-only; agent-step caps ⊆ identity ⊆ invoker) — no escalation down the tree, structurally.
2. **Sibling E_i isolated** (§2.2 rule 6): for_each/parallel instances have read-only ctx, no sibling communication; writes only via `collect`.
3. **E_i → D communication is the step return value only** (isolation consequence — an E_i cannot mutate D's stores directly).
4. **E_i are spawn-tree LEAVES** (v1): agent steps denied `delegate_to_agent` AND `run_pipeline`/`run_pipeline_inline` (R5 + S3) → no further children.
5. **Depth/width bounded by safety-limits** (S5): D + E_i count against A's spawn subtree vs `safety.spawn.max_depth/max_children`; static analysis computes the bound + checks at approval.
6. **Recovery asymmetry**: only D is recovery-tracked (session → `registry.restore_all` → resume from R4 snapshot); E_i are transient (completed step → result replayed from D's snapshot, LLM not re-run; in-flight-at-crash agent step → re-run fresh on resume, since its result wasn't journaled). D's step-level exactly-once absorbs all E_i recovery — E_i need no independent recovery.
`identity` expresses role (e.g. `reviewer`) but capability is always ⊆-bounded, so a different-identity agent step still can't exceed the pipeline's authority.

**Sync vs async — RESOLVED (owner 2026-07-04): BOTH, as separate tool names** (not a flag — matches §0.3 "structure not a mode field"). So the surface is up to 4 tools: `run_pipeline` / `run_pipeline_async` (registered) and `run_pipeline_inline` / `run_pipeline_inline_async` (ad-hoc) — collapsible if a combo (e.g. inline×async) is low-demand.
- **Sync** (`run_pipeline`): the caller (agent turn / TUI) stays ATTACHED and awaits. The pipeline's events stream LIVE to the caller (TUI shows current step + agent-step conversations = the Audit Event stream, i.e. N6 observability realized). **Ctrl-C / cancel propagates to the executor** (cooperative cancel at step boundaries, same pattern as router-loop `_is_turn_cancel_requested`); the R4 journal is intact on interrupt → clean abort OR later resume. Best for interactive/watchable pipelines. Owner's motivation: TUI can switch its conversation view to the running pipeline's progress + handle Ctrl-C.
- **Async** (`run_pipeline_async`): fire → detached driver-session runs independently, auto-resumes on crash, returns result via an inbox event (like delegation's `agent_response`). Events still journaled (TUI may subscribe later). Best for long/background/fire-and-forget.
- **Adds two impl requirements**: (1) an executor **cancel checkpoint** at step boundaries (small; reuse the router-loop cancel pattern), (2) **live event streaming** from the pipeline (driver D + agent-step E_i) to the caller/TUI (= the Audit Event stream + a live-render path = N6). Sync mode is what makes N6 observability load-bearing.
- The `ExecutionDriver.run_turn(user_text, chain_id)` interface (LLM-shaped) likely needs a more general entry for a pipeline driver — resolve when building the invocation slice.

## Downstream (unchanged from the proposal, now unblocked)
- **Total expression evaluator** = first code brick, implements R1 exactly (pure, tree-walker, fully tested vs appendix A/C). Explicitly not CodeAct AST.
- **Schema registry + validator** (E1) implements R2 (nested types).
- **Thin executor vertical-slice** implements R3 (linear pipe-data threading) + R4 (recovery) for linear `tool`/`agent`/`transform` steps only.
- Then A2 (spawn API) / B1 (narrowing) / D1 (`record_pipeline_state`, which R4 defines) are pulled by the slice.
