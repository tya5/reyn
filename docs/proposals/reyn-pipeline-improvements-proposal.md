# Reyn Pipeline — Improvement Proposal (spec + approach)

Deep design review of `reyn-pipeline-spec-v0.8.md` (+ `reyn-pipeline-reconciliation.md`, `reyn-pipeline-refactor-plan.md`), by lead-coder. Produced under a deliberate high-capability model pass; execution resumes under the normal model. **Uncommitted working note.** Status 2026-07-04: A1 (ExecutionDriver seam) merged (#2556); rest of the refactor + the executor are unbuilt.

## Verdict first
The core architecture is **sound and mature** — deterministic control plane vs non-deterministic execution plane, safety-by-structure (§0.3), a Turing-incomplete DSL with first-class static analysis, and a primitive set that is theoretically justified (appendix C) and empirically stress-tested (appendix A, 10 use-cases). I am **not** proposing architectural change. The improvements below close specific holes, resolve one internal contradiction, and correct the build sequence. The single biggest risk is not the design — it is **building the whole thing before the core loop is proven**; the approach section addresses that.

---

## Part 1 — Spec improvements (仕様)

### S1 [HIGH] — Control-plane persistence + exactly-once side effects (the crux; resolves §10 item 1 concretely)
§10 lists control-plane persistence as open, and §3.7/§7.2 simultaneously (over)claim crash-recovery "for free." This is the **load-bearing gap**, sharpened by the fact that **Pipeline replaces the task system, which today has WAL recovery** — the bar is "recovery no worse than tasks."
- **Session existence** is WAL-tracked, but the **pipeline's position** (current step, refine iteration, carry_forward, named-store contents) is separate state the WAL does not capture. And a naive "resume by re-running from the last step" **double-applies side effects** of `tool`/`shell` steps that completed their external effect before the crash — the classic at-least-once vs exactly-once problem the spec does not address.
- **Proposal**: control-plane state = a **step-boundary generation snapshot** reusing the proven `record_config_generation` pattern (`core/events/config_recovery.py`) — full-state, keyed at the durable WAL seq, truncation-surviving. **Each completed step (especially side-effecting tool/shell) journals its result BEFORE the executor advances.** On recovery, a step whose completion is journaled is **replayed from the journal, not re-executed**; execution resumes at the first un-journaled step. This makes the control plane deterministically recoverable *and* exactly-once for side effects.
- **Non-negotiable**: this ships with the **first** executor slice, with the CLAUDE.md-mandated truncate-falsify test — not deferred to §10.

### S2 [HIGH] — Commit to a TOTAL expression language; forbid restricted-code `transform` (resolve an internal contradiction)
Appendix B (line 761) already specifies `PRED,EXPR = deterministic: field refs, comparisons, all/any/count/join. No calls, no LLM` — a **total** (non-Turing-complete) language. But **§10 reopens it** ("transform の実行系: 式言語で足りるか、制限付きコードを許すか"). These contradict.
- **Resolve in favor of totality.** §7.3 (static enumeration of paths/cost/dataflow) and appendix C's own claim (line 860, "全域言語 / Dhall 系譜") both **require** the expression language to be total and statically analyzable. Restricted-Python has loops/comprehensions/unbounded recursion → it is **not** statically analyzable and **not** total → it silently destroys the spec's two central value props.
- **Sharp trap to name explicitly in the spec**: do **NOT** reuse Reyn's CodeAct safe-AST (`_validate_safe_ast` / `PURE_STDLIB_ALLOWLIST`) for `transform`. That gate is for `agent`-invoked Python (a different, Turing-complete trust context). A naive implementer will reach for it; doing so drags the whole non-total surface into the control plane. The `transform` evaluator must be a **separate, small, total combinator evaluator**.
- **Action**: fix appendix B/§10 to agree on the total language; specify the exact combinator set; **calibrate the expressiveness envelope empirically against appendix A (10 tasks) + appendix C (FP cases)** — because this is the single highest-leverage calibration (too weak → over-uses `agent`/`shell`; too strong → loses staticness; §1.1 makes `transform` the only free path, so pressure concentrates here).

### S3 [HIGH] — Close the `run_pipeline` nesting hole (protects the static cost upper-bound)
§3.2 forbids a step from starting another pipeline, but the **enforcement** is "the executor exposes no such API to steps." Yet §0.5/§7.1 make pipeline invocation a **tool call** (`run_pipeline`). If `run_pipeline` is in an `agent` step's `capabilities` (§3.4 allows any subset of the identity's caps), the step can **nest-trigger a pipeline at runtime** — escaping the static cost upper-bound (§7.3.4), the core guarantee, because the nested pipeline's cost is not in the parent's static envelope.
- **Proposal (structural, per §0.3)**: `run_pipeline` is **structurally non-grantable** to `agent` steps executing inside a pipeline. Nesting is expressed **only** via `call` (static literal target, cost-bounded, in the static envelope) or `match` (static case set) — never a runtime tool call. Add this to the Hard rules (appendix B rule set).

### S4 [MED] — Make side-effects-in-loops structural, not a recommendation
Rule 4 (line 775) *recommends* ("Prefer") performing external writes after a refine succeeds; nothing prevents a side-effecting `tool`/`shell` inside a `retry`/`refine` scope, which double-applies on each iteration/retry. Per §0.3's own philosophy (structure, not discipline): a side-effecting step inside a `retry`/`refine` scope should be a **static-analysis warning** unless annotated `idempotent: true` / `at_least_once_ok: true`. Complements S1 (S1 handles crash-replay; S4 handles loop-replay).

### S5 [MED] — Static analyzer computes spawn-tree bounds, checked against safety-limits at approval time
Operationalizes the owner's "integrate with the safety-limit system" note. Because the executor is session-typed and each `agent` step / `for_each` instance is a child session (§3.7), a `call`-nested or wide-`for_each` pipeline produces a deep/wide **spawn tree**. Extend §7.3 static analysis to compute **max spawn-tree depth + max concurrent sessions** and check them against the operator's `safety.spawn.max_depth` / `max_children` **at approval time** — so an over-cap pipeline is rejected before it runs (not aborted mid-execution). This turns the safety-limit integration into a static, pre-run guarantee.

### S6 [MED, v2] — Layered approval hash (own-body + callee bill-of-materials)
The transitive-closure approval hash (§7.1) means editing a shared leaf pipeline invalidates approval for **every** caller. For a compositional system at scale that is operationally painful. Propose splitting the hash into **own-body hash + a bill-of-materials of callee hashes**, so re-approval can be scoped ("callee X changed — re-approve this composition?") rather than monolithic. The `input` declaration (§5.7) already stabilizes interfaces; this stabilizes composition. Defer to v2, but record now.

### S7 [EDITORIAL, do before implementation] — Fold the reconciliation corrections into a spec v0.9
The spec's §7 will mislead implementers as written. Correct: "Global Journal" → WAL/`StateLog`; "Audit Event 非同期 pub/sub OTEL" → synchronous file-JSONL `EventStore` (no OTEL/pub-sub); per-tool "Allow/Deny/Ask" → axis allowlists + 4-layer approval (Ask is the absent-approval default, not a declarable value); "crash-recovery for free" → S1. Reframe §7 from "already conforms" → "reuses these existing mechanisms; the executor + expr-evaluator + analyzer are net-new."

### Affirmations (do NOT churn these)
- **The primitive set is complete.** Appendix C's pairings (match↔parallel, for_each↔fold, defunctionalization justifying static literals) are sound; I find no missing primitive. Do not add one speculatively.
- Timeout-as-control exclusion (rule 8), high-order-function absence (defunctionalization), the 3-part effect taxonomy, and the "1 pipeline = 1 completed task; long-lived state → event-driven trigger" granularity resolution (appendix A #5/#9) are all correct calls.

---

## Part 2 — Approach / sequencing improvements (進め方)

### P1 [primary] — Pivot from speculative reuse-refactor to a thin executor vertical-slice as the CONSUMER
A1 (ExecutionDriver seam, merged) cleaned a **real existing** seam — correct to do. But the rest of the refactor plan (A2 spawn-API, B1 narrowing-helper, D1 recovery-API, F1, E1) are **net-new functions whose only consumer is the unbuilt executor** → building them now is building ahead of the consumer (the "no production callsite = unproven / wrong-shape" risk). **Corrected sequence: A1 (done) → thin `PipelineDriver` + executor skeleton → A2/B1/D1 emerge from what the slice actually needs.** This is the "薄い縦切りで先に end-to-end" discipline, applied to the refactor plan itself. It supersedes the refactor-plan's front-loading of A2–F1.

### P2 — The first slice's definition-of-done includes crash-recovery (force S1 first)
The thin slice = **linear steps only** (`tool`/`agent`/`transform`; no `for_each`/`parallel`/`fold`/`refine` yet), in-process, unbounded budget — BUT it **must** include control-plane persistence + resume-after-crash + a truncate-falsify test (S1). Rationale: Pipeline replaces a recovering system, and S1 is the hardest design question; answering it in the first slice de-risks everything downstream. Do not ship a slice that "works but loses its place on crash."

### P3 — Build the total expression evaluator (S2) as the first standalone brick
It is pure (`(expr, context) → value`), independently testable, and foundational to both `transform` and the static analyzer. Validate it against appendix A + C before wiring it into the executor. This also reorders the refactor plan's E1 (SchemaRegistry) behind it — the expr evaluator is more foundational than the schema registry.

### P4 — Static analyzer built incrementally (completeness-by-construction), not big-bang §7.3
Each primitive, when added, ships its analyzer facet + its invariant test: `match` → path enumeration; `for_each`/`parallel` → cost bound + spawn-tree bound (S5); named-stores → dataflow graph. No primitive lands without its analysis contribution. This keeps "static analysis is first-class" true at every step and matches the reality that pipelines are LLM-generated (appendix B) — the analyzer is the validation net, so it must never lag the primitives.

### P5 — Gate task-system removal on proven Pipeline subsumption
Owner intent: Pipeline replaces the task system (possibly → external MCP task store). **Do not retire the recovering task system until the Pipeline slice demonstrably covers the real task use-cases** (a parity proof). Retiring a load-bearing recovering system before its replacement is proven is the exact "incomplete-work delays defect discovery" + "crash-recovery is the differentiator" risk. Make this an explicit gate in the plan.

### P6 — Spec v0.9 pass before the slice
Fold S1–S7 into a v0.9 so the slice is built against corrected truth (not §7's aspirational claims). Cheap, high-leverage, prevents implementer drift.

### P7 [anti-over-engineering guard] — Resist building the full analyzer / approval-hash / all-primitives before the linear+recovery loop is proven
The spec is ambitious (8 addenda). The right mitigation is incremental delivery (P1–P4), not cutting the design. Concretely: the approval-hash machinery (§7.1, S6), the full static analyzer (§7.3), and the non-linear primitives are all **later** — the first milestone is "a linear pipeline runs, is recoverable, and its cost/permission envelope is enforced."

---

## Recommended next actions (hand-off plan for the normal-model session)
1. **Spec v0.9 edit pass** — fold reconciliation corrections + S1–S7 into `reyn-pipeline-spec-v0.9.md`. Offload (docs-maintainer / a coder), lead reviews. Low risk, do first.
2. **Total expression evaluator** (S2/P3) — first code brick: a small total combinator evaluator (`core/pipeline/expr.py` or similar), pure + fully tested against appendix A/C, explicitly NOT the CodeAct safe-AST.
3. **Thin executor vertical-slice** (P1/P2): `PipelineDriver` implementing `ExecutionDriver` (built on the A1 seam) + a linear-step executor + step-boundary generation persistence (S1) + a truncate-falsify test. This is the consumer that shapes A2/B1/D1.
4. **Then** A2/B1/D1 (from the refactor plan) as the slice demands them — proven-by-use, not speculative.
5. Static-analyzer facets (P4) and non-linear primitives added incrementally, each with its invariant test.

The refactor-plan's A2→F1 chain is **not cancelled** — it is **re-sequenced to be pulled by the executor slice** rather than pushed ahead of it. E1 (schema registry) and C1 (per-session budget, the hard one) stay late.

---

## Part 3 — Non-security deep re-review (round 2, security dimension excluded by request)

A second pass focused on the DSL design, data/type model, expressiveness, and authoring experience — the parts under-examined in Parts 1–2 (which concentrated on recovery + correctness). These are **new** findings, distinct from S1–S7. Several are internal inconsistencies verified against the exact spec text.

### N1 [HIGH] — The schema/type system is flat, but the DSL references nested paths → contradiction
§5.6 schema fields are `{type: bool|string|number|enum[LABEL*]|list, required?}` — **flat**: no nested object type, and `list` has **no element type**. Yet the DSL pervasively uses **nested/dotted paths**:
- `match.on: "label.value"` (§2.5 line 304), `until: "ctx.review_consensus.passed"` (§4.2) — dotted paths into structure.
- `carry_forward: [PATH*]`, rule 7 "fields referenced by match.on/until/carry_forward **must exist in a declared schema**" — but a flat schema cannot declare `review_consensus.passed` or the element shape of a `list`.
- `for_each over: PATH` / `fold over: PATH` need the **element structure** to statically check `do`'s `{item}.field` references — impossible with an untyped `list`.

Real `agent` outputs are nested (`{approved: bool, feedbacks: [{file, comment}]}`). With a flat schema, either static analysis (§7.3) **cannot validate real outputs** (its central value prop), or authors **omit schemas** (§5.6 allows free-form) and lose all static checks — and then `match`/`transform`/`until` reference free-form data that can fail only at runtime.
- **Proposal**: extend the schema language with **nested object types and typed lists** (`list<REF>` / inline object fields). This is the prerequisite for §7.3's dataflow/path validation to be real. Decide this **before** the slice touches `match`/`verify`/`for_each` — it shapes the schema registry (E1), the expression evaluator, and the analyzer. (Appendix C's "no parametric polymorphism" is a *separate*, acceptable limitation — nested monomorphic types are still needed.)

### N2 [HIGH] — Pipe-data threading through compositional primitives is unspecified
The model is clean for linear steps ("each step receives the previous step's output," §5.1). But **what is the pipe data / output of a compositional primitive?**
- `for_each` (§2.2): `collect` runs once producing a result; the example gives it `output: audit_check` (a named store) — but does the for_each's *result also flow as pipe data* to the next step? Unstated.
- `match` (§2.5): the case targets are `{pipeline: bug-triage}` with **no `output:` at all** — the destination of a match's result is completely unspecified.
- `fold` (§2.4): `output: final_acc` (named store) — pipe data after? `parallel` (§2.3): `collect` with `output: merged`. `call` (§2.6): explicit `output:`.
- **Proposal**: state one uniform rule — **a primitive's return value (= its pipe data for the next step, and its `output:NAME` if declared) is: for_each/parallel → the `collect` result; fold → the final accumulator; match → the chosen target's final output; call → the callee's final output.** Fix the §2.5 `match` example to show where its result goes. Rule 5 already implies `output:NAME` doubles as the return/pipe value for plain steps; make that explicit and extend it to every primitive.

### N3 [HIGH] — The expression language is the least-specified, highest-leverage surface; its examples already exceed its stated grammar
Line 761 states the grammar as `PRED,EXPR = deterministic: field refs, comparisons, all/any/count/join. No calls, no LLM.` But the **examples use strictly more**:
- **lambdas**: `all(results, r -> r.verified)` (§2.2 line 242)
- **object/list literals**: `'{glossary: {}, summaries: []}'` (fold init, §2.4), `'{passed: ..., items: results}'`
- **string join over a projected list**: `join(review.comments, "\n")` (§4.2, canonical example)

So the real language includes higher-order iteration (`all`/`any`/`map`/`filter` with `x -> expr` lambdas), object/list construction, and projection — none of which line 761 admits. This is **both** an internal spec inconsistency **and** the crux calibration (it determines whether `transform` can do real glue, or whether every reshape falls back to `shell`/`agent`, defeating `transform`'s reason to exist per §1.1). Note this is the *expressiveness/consistency* angle — distinct from S2 (which was about totality/staticness at the same locus).
- **Proposal**: author a **precise standalone grammar** for the expression language as the **first spec deliverable**, covering the forms the examples already need (lambdas over bounded iteration, object/list literals, projection, `join`/`all`/`any`/`count`/`map`/`filter`) — and **re-verify totality + static-analyzability with those forms included** (bounded lambda-iteration over finite lists stays total; confirm no unbounded/recursive form sneaks in). Calibrate the combinator set against the reshape operations appendix A actually needs (map/filter/pluck/group/rename/merge — not just aggregate predicates). This refines P3: *spec the grammar precisely before building the evaluator*, because the current spec is self-contradictory on it.

### N4 [MED] — Pipeline discovery/surfacing to agents is unspecified
§5.7 says `description` is used "エージェントがパイプラインを道具として選択する判断材料" and appears "エージェントに提示されるパイプライン一覧に載る" — but the **surfacing mechanism is undefined**. A pipeline is invoked via a `run_pipeline` tool (§7.1); for an agent to call it, the agent must know **which pipelines exist and their input contracts**. This is the same problem just solved for skills (L1 name+description in the system prompt) and MCP/actions (`list_actions`/`describe_action`). 
- **Proposal**: reuse the pattern — a registered pipeline surfaces `name + description + derived input interface` (§5.7's derived `{ctx.*}` interface is exactly the "input schema" to show) into the agent's tool/capability view, mirroring the skill L1 block or the `list_actions` catalog. Do not invent a new surfacing mechanism.

### N5 [MED] — Expression/transform runtime-failure semantics undefined (compounded by optional schemas)
§6's error table omits **expression/transform evaluation failure**: what happens when a `transform`/`until`/`match.on` references an absent field or applies `count` to a non-list? Because §5.6 makes schemas optional (free-form output), these can only fail at runtime with no static catch.
- **Proposal**: define expression-eval failure as **step failure** (surfaced through the normal retry/error path), and add a static-analysis **warning** when `transform`/`match.on`/`until`/`carry_forward` reference an output that has **no declared schema** (you're navigating unvalidated structure). Pairs with N1 — nested schemas make most of these statically catchable instead.

### N6 [MED] — Authoring experience: dry-run/simulation + runtime path-trace
Pipelines are **LLM-generated** (appendix B is literally a "compact spec for generation") and the spec's own addendum 3 validated the design *by simulating generation*. Yet there's no specified **authoring/debug surface**:
- **Dry-run/simulate** (pre-approval): execute the control plane with **stubbed step outputs** (schema-conforming fakes) to confirm the dataflow actually produces the declared output and every path is reachable — catches "generated a pipeline whose carry_forward is never referenced / whose match label can't occur" before a real (costly) run.
- **Runtime path-trace**: since the control plane is deterministic, a trace showing *which* path was taken + the branch/transform values (from the Audit Event stream, §3.5) is the natural debugging view for "why did match go to default?"
- **Proposal**: make dry-run a first-class tool (it's cheap — no LLM, just the deterministic plane over stubs) and specify the path-trace as a documented consumer of the Audit Event stream. High leverage for a generated-artifact system.

### N7 [LOW-MED] — refine-scope ergonomics: partial refinement forces pipeline proliferation
§2.6: "refine の範囲指定問題は call で解決する" — to refine a *subset* of steps you must factor them into a **separate named pipeline** (+ registration + approval). This keeps the model clean (refine = always whole-pipeline) but pushes cost onto pipeline proliferation and approval churn. Defensible (composition over special syntax), but: **acknowledge the tradeoff explicitly and document the "extract-to-sub-pipeline-to-refine" pattern as first-class** (with a canonical example), so authors/generators reach for it deliberately rather than discovering the limitation. Not proposing new syntax.

### N8 [LOW] — Static validation of `tool`/`shell` args + `over` list-element types at approval time
Because `tool.name` and `args` structure are static (§3.6), the args can be validated **at approval time against the tool's declared input schema** (MCP tools carry input schemas; `ToolSpec.parameters` exists) — catching malformed tool calls before any run. Similarly `for_each/fold over: PATH` can be checked that the target is a typed list (needs N1). Fold these into the §7.3 analyzer as it's built (P4).

### N9 [LOW] — Confirm pipeline definitions are snapshotted at invocation (immutable-per-run)
Ties to §10's versioning item and hot-reload. State explicitly: a pipeline (and its transitive `call` closure) is **snapshotted at invocation start**, so a mid-run edit / hot-reload does not mutate an in-flight run — the run completes against the definition it started with. This is the pipeline analogue of the config-generation model and is required for the recovery contract (S1) to be coherent (replay must be against a fixed definition).

### N10 [LOW] — Unbounded `collect` input at scale
A `for_each over` a large runtime list feeds `collect` an N-element `results` list in one shot; if `collect` is an `agent`, that's an N-element context → token blowup (and the §10 "collect に agent" item). Note a **bounded/streaming/reduce-collect** as future work; for v1, document the guidance (prefer `transform` collect for large N; use `fold` when a running reduction is the goal).

### N11 [LOW] — Preset/recipe library for common shapes
The spec already anticipates "頻出パターンはプリセットとして提供" (old fetch/store). Given 12 constructs and LLM authoring, a small **recipe library** (agent→match→tool; fetch→agent→store; for_each-investigate→collect) reduces the generation surface and standardizes idioms. Low priority; an authoring aid, not a design change.

### Round-2 affirmations
- The **granularity resolution** (exploration lives inside one `agent`; the control plane only checks goals — §0.6, granularity rule) is the spec's best conceptual call and should be preserved verbatim.
- The **effect taxonomy** (agent/transform/tool = non-deterministic/pure/effectful) and its FP grounding (appendix C) are clean; no primitive is missing.

### Round-2 approach refinements (fold into the Part 2 plan)
- **The two "spec-first" bricks are N1 (nested schema decision) and N3 (precise expression grammar)** — both are *internally inconsistent in the current spec* and both gate the evaluator + analyzer + verify. Resolve them in the v0.9 pass (P6) **before** the slice, not during it.
- **N2 (uniform pipe-data rule)** is a small but essential v0.9 edit — the executor slice can't be built without a defined "what's the input to the next step after a primitive."
- N4/N6 (surfacing + dry-run) are the highest-value *authoring-experience* additions and should be scheduled once the linear slice runs — dry-run especially, since it's cheap (deterministic plane over stubs) and directly serves the generated-pipeline workflow.

**Net**: Part 3 surfaces three HIGH internal-consistency/expressiveness gaps (N1 flat-schema-vs-nested-paths, N2 undefined pipe-data threading, N3 under-specified expression grammar) that the v0.9 spec pass must close before implementation, plus authoring-experience investments (N4 surfacing, N6 dry-run/trace) that make the LLM-generated-pipeline workflow actually usable. None require architectural change.
