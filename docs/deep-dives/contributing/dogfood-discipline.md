---
type: contributing
topic: dogfood-discipline
audience: [human, agent]
---

# Dogfood Discipline Guide

A pedagogical reference for developers and agents new to Reyn who want to understand and reproduce the discipline established across dogfood batches 7–14.

---

## 1. Why this discipline exists

### The gap between test green and real use

A test suite can be fully green while the product is unusable. This is not a Reyn-specific problem — it is a universal gap between **invariant satisfaction** and **user experience**. Tests verify that specific contracts hold. They do not verify that the system behaves coherently as a whole, under real conversational inputs, with a probabilistic LLM making decisions at every phase.

For Reyn specifically, the gap manifests in two forms:

**1. LLM-driven workflows have probabilistic failure modes that unit tests cannot capture.**
An OS invariant test verifies that the transition validator correctly rejects an illegal next-phase. It does not verify that the LLM, given a real system prompt and a real user message, will actually choose the correct next phase. The latter depends on the structural environment Reyn constructs (schema constraints, context injection, preprocessor delegation) and on the LLM's probabilistic behavior within that environment. Only end-to-end execution reveals whether that environment is sound.

**2. Test fixture drift is a silent danger.**
A test that passes with a handcrafted fixture may fail when the OS generates the real artifact at runtime, because the fixture no longer matches the real output shape. This is not a hypothetical — it was observed directly in batch 9 (the "wrong layer trap", described in Section 3, Principle 6).

The implication: test green is a necessary but not sufficient condition for "Reyn works." Dogfood is what bridges the gap.

### What dogfood means here

"Dogfood" in this context means: **running Reyn's own stdlib skills through `reyn chat` and observing what actually happens** — not what tests predict, not what static analysis shows, but what the LLM does with the actual system prompt, the actual context, and the actual artifact flow.

The observation unit is a **scenario**: a concrete user message that exercises a specific code path. Scenarios are grouped into **batches**. Each batch ends with a retrospective that extracts learnable principles and feeds them into the next batch's design.

This is a form of structured empiricism. The discipline is how you make that empiricism systematic, reproducible, and incrementally useful.

### Connection to Reyn's design vision

Reyn is designed for **predictability over autonomy** — specifically for deployment contexts where unexpected behavior carries high cost (see [Principles P1–P8](../concepts/principles.md)). That vision only has meaning if "predictable" is measured against real workloads, not just synthetic test fixtures. Dogfood is the measurement instrument.

---

## 2. The iterative loop — structure of one batch

Each batch follows the same five-step structure. The structure is not bureaucratic overhead; each step serves a specific purpose that cannot be collapsed into another.

### A1: Draft scenario plan

The assistant (or engineer running the batch) drafts a list of scenarios to run. Each scenario specifies:
- A concrete user message (not a test ID)
- The code path it exercises
- The expected outcome (expressed as a probability distribution, not a binary)

The draft is the **explicit statement of hypotheses**. Writing it forces you to articulate what you expect to happen — which makes the gap between expectation and reality visible during A4.

The expected outcome should use the four-category format established in batch 8:
- **verified**: fix is effective / behavior matches expectation
- **inconclusive**: observation is ambiguous or blocked by another layer
- **refuted**: behavior contradicts expectation
- **blocked**: the scenario's observation path is obstructed by a prior bug

Including "blocked" is important. It is the most common miss when calibration is new — a scenario that was predicted as "60% verified" turns out blocked because the chain doesn't reach the relevant phase. Without the blocked category, that outcome has no place in your prediction model and calibration is distorted.

### A2: User review (mandatory — skip is not permitted)

The scenario plan is reviewed before execution. This is the last point at which a **design-level intervention** can redirect the batch before cost is spent.

The value is not primarily error correction; it is **forcing articulation of design intent**. A user who reads "expect 60% verified for G15 fix" and replies "wait, did you check whether the test fixture matches the runtime artifact shape?" has just prevented a wrong-layer trap — before running any scenarios.

A2 is also where the implicit simplicity check happens. If the description of expected behavior is difficult to summarize, that is often a signal that the underlying design has accumulated incoherence (Section 3, Principle 9).

### A3: Parallel execution with worktree isolation

Scenarios are executed by sub-agents dispatched in parallel. Each sub-agent operates in an isolated worktree with its own `.reyn/` state directory. This isolation is essential:

- State collisions between concurrent scenario runs are structurally impossible
- A failure in one scenario does not corrupt the observation context of another
- Sub-agent parallelism reduces total wall-clock time without reducing per-scenario fidelity

In Reyn: each `sonnet` sub-agent receives a fresh worktree and runs `reyn chat` with the scenario's user message via piped stdin.

For systems outside Reyn: the equivalent is per-scenario process isolation. Any shared state (model cache, temp directories, event logs) must be either isolated or treated as a confound.

### A4: Findings aggregation and review

After all scenarios complete, findings are aggregated. Each finding is classified by severity:
- **CRITICAL**: system non-functional
- **HIGH**: core user path blocked
- **MED**: degraded behavior, workaround exists
- **LOW**: cosmetic or edge-case

The aggregation is shown to the user before any fix dispatch. This is the **"user's sense check"** step: the user reads the findings summary and identifies whether the observed behavior matches their intuitive model of the system. Discrepancies here are often the most valuable signal. A user who says "wait, that shouldn't be possible given how X works" may be detecting a wrong-layer symptom (Section 3, Principle 6) before the engineer does.

### A5: Bug classification, fix wave or deferred

Each HIGH and CRITICAL finding enters one of two tracks:
1. **Fix wave**: a set of parallel fix dispatches targeting confirmed, reproducible bugs
2. **Deferred (giveup tracker)**: bugs that cannot be fixed in this batch because of structural dependency, design ambiguity, or non-determinism that requires more data

The classification discipline (Section 3, Principle 7) applies here: every fix must be labeled as either a **spec change** (visible behavior change, requires user notification) or a **bug fix** (restoring documented design, no user-visible change to intended behavior).

### Retrospective: lesson extraction and handoff

After the batch's fix wave completes and the next retest verifies, a retrospective is written. The retrospective has a fixed structure:
- Expected vs actual (what the A1 plan predicted vs what happened)
- Turning points (unexpected events that changed the batch's direction)
- Principles reinforced or newly established
- Handoff to next batch (open questions, carry-over findings, calibration adjustments)

The retrospective is the batch's **durable output**. Scenarios and findings are operational records. The retrospective is where learnable principles are extracted and made available to the next batch's A1 planning.

---

## 3. Nine-principle framework

These nine principles were established through repeated observation across batches 7–14. Each is presented in two parts: the universal formulation (applicable to any LLM-driven system) and a conceptual example illustrating the principle in a realistic context.

---

### Principle 1: Separate deterministic from non-deterministic work

**Universal principle.** Every LLM-driven workflow phase can be decomposed into two categories of work: work that is a pure function of the inputs (deterministic), and work that requires judgment, weighing alternatives, or generating novel content (non-deterministic). The error is mixing the two in a single LLM act loop.

When deterministic work is left to the LLM, the LLM does not treat it as mechanical — it treats it as a judgment call. Weak LLMs in particular will skip or incorrectly execute file writes, path computations, and schema validations not because the instructions are wrong, but because those operations are structurally indistinguishable from the judgment steps that surround them.

The design rule: **any work that can be written as a pure function — file glob, path derivation, list filter, schema validation, format conversion — belongs in a phase preprocessor or a deterministic op, not in the LLM act loop.**

**Conceptual example.** A phase that "reads input files, transforms them, and writes output files" has zero judgment content. Every output path is derivable from input paths. Every write is derivable from the transform rule. If this phase is LLM-driven, the LLM will probabilistically skip writes, over-broaden globs, or repeat reads — not because the instructions are unclear, but because those operations are deterministic and the LLM is optimized for judgment. Moving the logic to a preprocessor eliminates the failure mode structurally: the LLM call count drops to zero and `max_act_turns: 0` can be set.

The checklist: (1) Is the output derivable as a pure function of the input? If yes, the phase is a preprocessor candidate. (2) What are the actual judgment steps? Enumerate them explicitly. (3) Are any non-judgment steps currently in the LLM act loop? Remove them.

See: `feedback_deterministic_split.md`

---

### Principle 2: Prompt design — avoid bloat, avoid over-consolidation

**Universal principle.** System prompt rules accumulate as scenario-specific fixes. Each fix adds a rule targeted at the failure mode that triggered it. Over time, the rule set grows and rules begin to encode overlapping intents with subtly different wording. This is prompt bloat, and it causes two failure modes: (a) rules contradict or overlap, confusing the model; (b) adding a rule for scenario A degrades behavior in scenario B because the rule is over-specific.

The counter-move — consolidating many rules into fewer paragraphs — creates the opposite failure mode. A weak LLM treats a multi-sentence paragraph with one MUST as lower priority than four separate bullets each with their own MUST. Consolidation weakens the signal.

The optimal balance: **individual bullet per rule × one MUST per bullet × wording deduplication across bullets**. Bullets stay separate; wording is deduplicated within each bullet; the MUST is never merged across bullets into a paragraph.

**Conceptual example.** Three rules targeting the same workflow (list skills → describe → invoke) accumulate as three separate bullets. Consolidating them into a single paragraph introduces a regression: the model honors the paragraph as one unit at lower priority than the original three individual MUST signals. The fix: keep three bullets, deduplicate any shared phrasing within each bullet, but do not merge the bullets into a paragraph.

Audit trigger: every time a new prompt rule is added, check existing rules for intent overlap. If two rules encode the same behavioral intent, deduplicate wording — but keep them as separate bullets.

See: `feedback_prompt_design.md`

---

### Principle 3: Halt speculation — one hypothesis, one fix, one verification

**Universal principle.** When diagnosing an LLM behavior failure, the temptation is to bundle multiple hypotheses into a single "comprehensive fix" — reasoning that if all plausible causes are addressed simultaneously, the problem will definitely be solved. This approach has two costs: it multiplies the work, and it destroys the learning. When a bundle of fixes works, you cannot tell which hypothesis was correct. When it fails, you have invested in several fixes with no signal on which ones to try next.

The discipline: **isolate one hypothesis, make the smallest change that tests it, observe, decide**. Then move to the next hypothesis.

**Conceptual example.** A phase produces incorrect output. Three hypotheses: (a) field naming prevents the LLM from recognizing the field; (b) the schema does not declare the field explicitly; (c) the instruction does not reference the field. Bundling all three into one fix takes an hour and produces one bit of information: the bundle worked or didn't. Testing hypothesis (a) alone takes five minutes (rename one field, rerun). If it works, hypotheses (b) and (c) are unnecessary. If it doesn't, move to (b). The total cost is lower and the learning is higher.

Ordering: test hypotheses in order of cheapest observation cost first. A field rename is cheap. A schema extension that requires artifact contract changes is expensive. Always verify cheap first.

See: `feedback_minimize_speculation.md`

---

### Principle 4: Build observation infrastructure before speculating about LLM behavior

**Universal principle.** LLM behavior hypotheses — "the model ignores this field," "the model misidentifies this skill," "the model is stuck in this attractor" — cannot be confirmed or refuted by reading code. They can only be confirmed by observing what the LLM actually receives and what it actually produces. Without observation infrastructure, all LLM behavior analysis is speculation. Speculation stacks: each unverified hypothesis becomes the premise for the next, and the stack self-reinforces until a contradicting observation demolishes it.

The discipline: **when you first suspect LLM behavior, ask whether you can observe the LLM's input payload and output.** If the infrastructure to do so does not exist, build it before forming hypotheses.

**Conceptual example.** A finding states "the router is misidentifying skill names." Four hypotheses are proposed: (a) enum constraints are missing; (b) skill descriptions are truncated; (c) a prompt rule was inadvertently removed; (d) the model hallucinates names it has seen in similar contexts. Without observation infrastructure, all four are plausible and a comprehensive fix addresses all four. With observation infrastructure (dump the actual system prompt, inspect the enum, replay the payload), three of the four can be eliminated in minutes. The correct fix targets only the confirmed cause.

After building observation infrastructure, retroactively verify all previous hypotheses. The historical pattern from Reyn's batch 7: 4 prior hypotheses were evaluated retroactively with the new tooling, and 1.5 were refuted — meaning fixes based on those hypotheses would have been wrong-layer.

See: `feedback_observe_before_speculate_llm.md`

---

### Principle 5: Care boundary — structural, behavioral, gray

**Universal principle.** Every design decision in an LLM-driven system can be classified into one of three categories based on when in the LLM call lifecycle it operates:

1. **Structural (pre-call care, always do):** Building the environment in which the LLM will make its decision. Schema constraints, context injection, deterministic preprocessing, input shape normalization. These are mandatory — without them, the system cannot function. Structural changes have deterministic effects.

2. **Behavioral rescue (post-call, never do):** Rescuing or patching the LLM's output after the fact. Auto-retry, fallback escalation, attractor state machines. Behavioral rescue is a bloat trap: each new failure mode requires a new rescue arm. It also hides failures from the event log and from the user, making the system harder to debug. LLM probabilistic failures should be surfaced visibly, not silently corrected.

3. **Gray zone (prompt rules, handle with care):** Pre-call but behavioral in nature. Prompt rules ask the LLM to voluntarily honor a constraint. They are sometimes necessary but carry the accumulation and over-consolidation risks described in Principle 2.

**Conceptual example.** An LLM repeatedly produces an empty output. Three candidate responses: (a) add an enum constraint to the output schema (structural — prevents the empty output structurally); (b) add auto-retry logic in the OS when empty output is detected (behavioral rescue — hides the failure); (c) add a MUST rule to the system prompt (gray zone — may work, may bloat). The correct response is (a). If (a) is not applicable (the output is genuinely optional), the correct response is to emit a structured event and surface it to the user — never (b).

The classification question for every fix: "Is this structural preparation, post-call rescue, or a gray-zone prompt rule?" The answer determines the correct fix layer.

See: `feedback_reyn_care_boundary.md`, [care-boundary.md](../concepts/care-boundary.md)

---

### Principle 6: Verify-first and reproduce-first

**Universal principle.** Two gates must be passed before any fix is declared "landed":

**Reproduce-first gate:** Before investing in a fix, confirm that the bug actually reproduces on the current HEAD. Bug observations are made at a specific moment in a specific run. After other fixes land, a previously observed bug may no longer reproduce — not because it was fixed directly, but because the upstream condition that triggered it was eliminated. These are **resolved-indirectly** findings. Skipping the reproduce gate means investing fix effort in bugs that no longer exist.

**Verify-first gate:** After a fix lands and tests pass, confirm that the fix is effective end-to-end in a real dogfood scenario. A test passing is not sufficient. The test fixture may not reflect the actual artifact shape the OS generates at runtime (the "wrong layer trap"). Only an e2e observation confirms that the fix reaches the actual failure point.

**Conceptual example of wrong layer trap.** A test fixture is written as `{"type": "unknown", "data": {"target_skill": "..."}}`. The OS at runtime generates `{"eval_spec": {...}, "target_skill": "..."}` — the `data` wrapper is absent. A fix that checks `data["target_skill"]` passes the test (because the fixture has the wrapper) and fails at runtime (because the real artifact does not). The test is testing a wrong layer. Only e2e verification reveals this.

**Resolved-indirectly classification.** When a bug does not reproduce, it is classified as resolved-indirectly and documented with: (a) which upstream fix caused the resolution; (b) why the observation was a downstream symptom rather than a root cause. This documentation prevents the same false bug from being re-investigated in future batches.

Historical calibration data: in batches 9–10, 2 of 3 candidate bugs were resolved-indirectly. Brier score improved from 0.96 (batch 8, no verify/reproduce gates) to 0.30 (batch 10, both gates applied).

See: `feedback_verify_reproduce_first.md`

---

### Principle 7: Classify fixes explicitly — spec change vs. bug fix

**Universal principle.** Every fix dispatch should be labeled with one of two classifications:

- **Bug fix (restoring documented design):** The documented specification exists and was violated by a prior change. The fix restores the system to spec. No user-visible behavior change is intended. Production deployments are not affected.
- **Spec change (new or modified behavior):** The specification is being extended or altered. User-visible behavior changes. Production deployments may need to be informed.

The discipline to classify is not overhead — it serves a concrete purpose: it tells you whether to audit the documented design before dispatching the fix.

**The audit implication:** If a fix is classified as a bug fix (restoring documented design), the first step is to confirm that the documented design actually specifies the behavior being restored. If the relevant spec is ambiguous or absent, the fix cannot be classified as a bug fix — it is a de facto spec change and should be treated as one.

**Conceptual example.** A permission system fix is dispatched as "add auto-approval for non-interactive contexts." Before dispatching, checking the permission model spec reveals that the documented design describes four approval mechanisms (config file, CLI flag, approvals file, interactive prompt) with no auto-approval variant. The "fix" is actually a spec change — and one that introduces asymmetric behavior not present in the documented model. The correct response is to reject the fix and instead identify what documented behavior is actually broken.

This principle was established in batch 13, triggered by a user simplicity test: "Can you describe the permission system in a few sentences?" The inability to produce a concise description was the signal that accumulated fixes had introduced undocumented behavior.

---

### Principle 8: Documented design coherence audit

**Universal principle.** Over multiple fix batches, accumulated changes can drift the implementation away from its documented design. No single fix introduces a large incoherence — each fix is locally reasonable. But the cumulative effect is a system where the behavior can no longer be explained by the documented principles.

The audit discipline: **before dispatching a fix batch, read the relevant specification documents and confirm that the proposed fixes are consistent with the documented design.** This is the architectural equivalent of Principle 4 (build observation infrastructure before speculating): you should not speculate about what the correct behavior is without first reading the documented model.

The **simplicity smell test** is the user-side heuristic for triggering an audit. If someone who understands the system cannot describe a component's behavior in two to three sentences, that is a signal that the component has accumulated incoherence. The simplicity test is not a formal check — it is a conversation-level detector that precedes the formal audit.

**Conceptual example.** A system has accumulated three fixes to its permission model over several batches. Each fix was internally consistent at the time it was dispatched. A user asks for a simple description of how permissions work. The response requires five rules and one exception to the rules. The exception is a sign: the exception was probably introduced by a fix that violated the underlying symmetry of the model. The audit finds the offending fix, classifies it as a doc-violating change, and reverts it.

After the revert, the permission model can be described in three rules with no exceptions — and its behavior is predictable.

---

### Principle 9: Simplicity smell test

**Universal principle.** Accumulated fixes can produce a system that is locally correct (every individual piece can be justified) but globally incoherent (the system as a whole cannot be explained simply). The simplicity smell test is a heuristic for detecting this condition before it progresses further.

The test: **can the component's behavior be described in one or two sentences, with no exceptions?** If not, one of two things is true: (a) the component is genuinely complex and the description requires depth; or (b) accumulated fixes have introduced asymmetries and exceptions that don't belong. Distinguishing (a) from (b) requires reading the documented design. If the documented design is simple but the current behavior requires exceptions, (b) is confirmed.

**Design symmetry as the positive criterion.** A well-structured component has symmetric behavior: the same principle applies uniformly, with no special cases for particular invocation modes or contexts. Asymmetric behavior — "in this mode it works this way, in that mode it works differently" — is the positive signal for incoherence.

**Conceptual example.** An approval mechanism works one way when called interactively (user sees a prompt) and a different way when called non-interactively (auto-approves silently). The asymmetry is justified at the time it is introduced ("non-interactive can't show a prompt"). But the documented design treats approvals uniformly regardless of invocation mode. The simplicity smell test flags the asymmetry; the audit confirms it violates the documented model. The correct fix is not to extend the asymmetry further but to find a symmetric mechanism that works in both modes.

This principle complements Principle 8 (audit) by providing the trigger signal. Principle 8 tells you how to audit; Principle 9 tells you when to audit.

### Principle 10: Structural pre-check before attractor naming (= batch 17 lift)

**Symptom-class principle.** Before naming a behavioral observation as an "attractor" (= LLM picks the wrong path despite available alternatives), confirm that the intended path is actually present in the LLM's view: the tool is in the catalog, the dispatch route is wired, and the candidate is in `candidate_outputs`. A 0/N invocation rate caused by missing wiring looks identical to an attractor but has a completely different fix path (= structural code change, not prompt fix).

**Operational rule.** Each scenario in the prelude declares its **structural pre-check status** (= ✓ / ⚠️ / ❌) before the behavioral prediction is made. If structural pre-check fails, the scenario records `verdict=blocked` rather than `refuted`; this protects the calibration record from polluting the attractor base rate with structural bug data.

Case study: batch 17 (= ADR-0033 RAG Phase 1 first dogfood) classified S5 0/5 invoke as an attractor, then discovered the underlying issue was 3-layer wiring drift (`ToolRegistry` registered + `build_tools()` not + `_REGISTRY_DISPATCH_TOOLS` not) — three independent boxes that all had to ✓ for "the tool is callable from the chat router". `feedback_observe_before_speculate_llm.md` already counsels building observation infra before speculating; principle 10 is its dual: build a deterministic structural check before observation produces stochastic data you cannot interpret.

### Principle 11: Separate structural and behavioral prediction axes (= batch 18 lift)

**Symptom-class principle.** A scenario's verified-rate prediction is the product of two independent axes, not a single number: (a) **structural axis** = "is the intended path present in the LLM's view?" (= deterministic, binary, pre-checkable), and (b) **behavioral axis** = "given the path is present, will the LLM pick it?" (= stochastic, base-rate-dependent, requires N runs). Conflating them produces optimistic predictions when a structural fix lands ("we fixed the wiring, so verified should jump to 70%+") that the behavioral base rate does not actually support.

**Operational rule.** Prelude predictions for each scenario declare two rows: a structural-axis row (= pre-check status, prediction = ✓/⚠️/❌) and a behavioral-axis row (= attractor base rate from prior batches, prediction = X%). Verified prediction is then `P(structural ✓) × P(behavioral ✓)`. This catches the "structural fix landed → optimistic prediction → calibration miss" trap that recurs after major fix waves.

Case study: batch 18 retest after batch 17's 5-commit fix wave landed all 6 release-blocker structural fixes and predicted 70-75% verified across 4 scenarios. Structural axis was 100% confirmed (= every fix was wired at the intended layer). But verified came in at 25% (= 3/12 primary), because the behavioral axis surfaced new attractors that the structural fix didn't address (= S6 R-RAG-srcread, S9 R-RAG-numerical-vs-flag-bias, S8 verification-path gap from `reyn web` `interactive=False`). The prediction would have been 25-40% if the two axes had been multiplied separately.

### Principle 12: Verdict false-attribution discipline (= batch 18 lift)

**Calibration principle.** "Refuted" is not a catch-all for non-verified runs. False attribution between verdict categories pollutes the calibration record and produces wrong fix-path conclusions. Three distinctions matter:

- **`refuted`** — the LLM had the path available and picked something else (= R-attractor data point; legitimate prompt / schema / model fix candidate)
- **`inconclusive`** — the LLM picked the intended path correctly but the verification harness couldn't observe completion (= driver / harness / config gap, not LLM behavior)
- **`blocked`** — the structural pre-check itself failed (= structural bug, predates behavioral measurement)

**Operational rule.** Verdict assignment in the driver is explicit; per-run docs cite the specific evidence (= "tool was invoked but reyn web's `PermissionResolver(interactive=False)` short-circuited the ask cycle, so verification path unreachable" → inconclusive). Cross-batch attractor base rates are computed only from `refuted` runs, never mixing in inconclusive or blocked.

Case study: batch 18 S8 (= drop_source via chat) saw the LLM correctly invoke `drop_source` in 3/3 runs, the permission-denied event fired correctly in 3/3, the wiring worked end-to-end. The problem was that `reyn web` constructs a non-interactive permission resolver, so the intended ask-and-approve cycle short-circuited to deny. Calling this `refuted` would have created phantom evidence for a "drop_source attractor" that does not exist. Marking it `inconclusive` correctly attributes the issue to a UX config gap (= R1 carry-over) instead.

### Principle 13 (candidate): Behavioral attractor class taxonomy

> ⚠️ **Status: partial evidence — Class A confirmed, Class B hypothesis pending, Class C is established prior knowledge.** Case study and scope are recorded here for future replication; do not generalise beyond confirmed evidence.

**Hypothesis.** Behavioral attractors subdivide by what produces the wrong path, and the effective fix layer follows the class:

- **Class A — Cognitive-bias attractor** (✅ valid evidence, batch 19 S9): the LLM has all the input it needs but weighs evidence wrongly (= numeric value over boolean policy flag). Fix layer: **prompt-layer "named anti-attractor callout"** of the form *"Common attractor to avoid: when X, do NOT Y. Z wins over W."* Compliance was ~100% in S9, with smoking-gun evidence (= LLM cited the small numeric value in its reasoning while still emitting the abort decision).
- **Class B — Affordance-bias attractor** (⚠️ hypothesis pending): when multiple tools / sources can plausibly handle a query, the LLM may default to one and stop. Three batches (= 18, 19, 20) attempted to gather valid evidence; each surfaced a different scenario-design confound. The decisive-judgment scenario specification is now recorded (= prompt must structurally require both sources, e.g. *"Give me (a) the conceptual overview AND (b) the actual class names I'll need to import"*) but the retest itself is post-1.0 fast-follow scope. Until a valid scenario produces the data, Class B remains a hypothesis.
- **Class C — Protocol-level attractor** (✅ valid evidence, prior G12): LLM API protocol-level quirk (= post-tool empty-stop, format leak, role artifact). Fix layer: **envelope-layer adapter pattern** (see `feedback_envelope_layer_fix.md`).

**Intervention layer ladder** (= cheap → expensive): prompt-layer → schema-layer → envelope-layer → model-layer. Class A fits prompt-layer; Class C fits envelope-layer; Class B (when validated) is hypothesised to require schema-layer or beyond, but this is unverified.

**Why partial evidence matters.** The cost of declaring Class B prematurely is real: batch 19 retrospective initially named the taxonomy as established and proposed schema-layer escalation, then `feedback_pre_retrospective_discipline.md` (= principle batch 19) caught the over-generalisation when re-reading the LLM trace dumps. Recording the hypothesis with explicit evidence-status saves future readers from inheriting a confident-sounding but unsupported claim.

### Principle 14 (candidate): Scenario design audit checklist

> Status: established by batch 20. Four-dimension audit replaces the implicit one-dimension audit that produced three consecutive scenario-design flaws (batches 18-20).

**Operational rule.** Every prelude scenario declares its design audit across four dimensions:

| Dimension | Audit point | Mitigation example |
|---|---|---|
| 1. Data semantic match | Does the indexed source / data content cover the prompt's topic at the depth the prompt asks for? | If the prompt asks "how is X implemented?" but the source is concept-only, the data does not match |
| 2. Tool affordance match | Do related tool descriptions claim the prompt's exact use case? Does that conflict with the expected verdict? | `reyn_src_read` description claims "for any 'how does Reyn X work?' question" — a prompt of that shape will route to it correctly, even if the test expected `recall` |
| 3. Structural source-count requirement | Does the prompt structurally require the expected number of sources? Or could a single source rationally satisfy it? | "How does X work?" rationally satisfies from concept doc alone; testing multi-source picks requires a prompt that has content unique to each source |
| 4. Rational alternative paths | What other tools / paths can rationally handle this query? Is the expected path actually the most rational, or are alternatives stronger? | If web_search or file_read can naturally answer, the expected path may not be the LLM's rational choice |

A scenario is approved for execution only when all four dimensions are ✓; ⚠️ on any row triggers redesign before the prelude is committed.

Case study: batch 18 surfaced dimension 1+2 (= S6 prompt "How is recall implemented?" failed both — concept-only data did not match the implementation question, and `reyn_src_read`'s description claimed the exact use case). Batch 19 fix wave addressed only the surface symptom and produced no new evidence. Batch 20 redesigned with synthetic sources to break dimension 2 (= reyn_src_read could not answer the fictional "Quantum Bridge Protocol" prompt) but failed dimension 3 (= "How does X work?" was still a single-source-sufficient query). The fourth dimension (= rational alternative paths) was implicit but uncodified — codifying it here closes the audit checklist.

**Why the checklist is the lift, not the redesigns.** Each individual scenario redesign in the batch-18-to-20 sequence felt locally correct; the systemic flaw was that audit was one-dimensional. Once the four dimensions are explicit in the prelude template, future scenarios catch the gap before they consume a batch budget.

### Principle 15 (candidate): Prompt class taxonomy

> Status: established by batch 21 (= real e2e dogfood). The 83% verified rate
> in batch 18 S5 was driven by an explicit-search hint in the prompt; the
> same scenario with a natural concept query produced 0%. Dogfood scenarios
> must declare their prompt class so prediction base rates are calibrated
> per class.

**Operational rule.** Each scenario in the prelude classifies its prompt as one of:

- **Class P-explicit** — the prompt contains an explicit search / lookup / find verb (= "Search the docs", "look up X", "find the X for me"). The user's intent is tool-level: they want retrieval, not narrative answer. The router system prompt's "When user says 'search' / 'find in docs' / 'lookup', use recall" rule fires.
- **Class P-natural** — the prompt is a natural question without tool-level verbs (= "What is X?", "Explain X", "How does X work?"). The user's intent is content-level: they want an answer, not a tool invocation. Tool routing has to be inferred from context (= tool descriptions + SP rules + indexed source descriptions).

The two classes have **different base rates for any given attractor**. Batch 18 S5 P-explicit hit 83% verified; batch 21's same scenario P-natural hit 0% before the schema-layer fix. Predictions written without classifying the prompt anchor on whichever rate the dogfooder happens to remember and produce systematic miscalibration.

Case study: batch 18 S5's verified rate was treated as the headline metric for "RAG is working" until batch 21 (= real e2e against `docs/concepts/*.md`) showed natural concept queries returned 0/3 with hallucinated paths. The 83% number was real, but it described P-explicit class; the gap to real-world UX was the absence of P-natural class measurement.

**Implication for prelude predictions.** Prediction rows split by class when both could apply:
- Structural axis: same for both classes (= principle 11)
- Behavioral axis P-explicit: prior batches' explicit-search base rate
- Behavioral axis P-natural: prior batches' natural-question base rate (= often much lower until schema-layer fixes land)

### Principle 16 (candidate): Pre-fix multi-agent context analysis

> Status: established by batch 22 (= affordance-bias schema-layer fix). Lifts
> the pre-retrospective discipline (= principle batch 19) one phase earlier:
> before designing a fix, dispatch parallel info-gathering agents so the fix
> design starts from evidence, not speculation.

**Operational rule.** When designing a fix for a behavioral attractor (= principle 13 Class A / B / C), dispatch parallel sonnet agents in **info-gathering only mode** (= no edits, read-only) before writing any code. A typical fan-out is 5 agents covering:

1. **Trace deep-dive** — read all trace dumps for the attractor, plus a comparison batch where the same surface verified, identify the smallest LLM-input-level structural difference.
2. **Industry research** — how do mainstream agent frameworks (= OpenAI, Anthropic, LangChain, MCP, practitioner blogs) describe the same affordance conflict? Are there documented patterns?
3. **Description / rule history audit** — git blame the existing description / SP rule, find the commit and motivation, list the use cases the original wording protects.
4. **Constraint audit** (= reverse direction) — what existing surfaces (empty-state, vocab, required fields, B17 disambiguations) must any fix preserve?
5. **Design space mapping** — enumerate ALL the levers (tool description, SP rule, parameter schema, tool ordering, conditional suppression, category field, strict mode, empty-state suppression) and rank them by effort × evidence × risk.

The main agent then synthesizes the 5 reports and lands a multi-layer fix in **one commit**, instead of iterating on prompt-tweak speculation.

Case study: batch 22 (= affordance-bias schema-layer fix). Batches 18-20 spent 4 attempts iterating on prompt rewrites and synthetic-content scenario redesigns, all 0/3 verified. Batch 22 ran 5 parallel context-analysis agents, discovered the true driver was a SP-level rule (not the tool description as initially assumed), and landed a multi-layer fix (SP rule + 2 tool description rewrites per practitioner 4-part template) in one commit. Same scenario, same prompts, same N=3: 0/3 → 3/3 verified, first attempt. The cost of the extra synthesis stage (= ~10 min wall-clock for parallel info-gathering + ~5 min synthesis) is recovered many times over compared to the 4 hours spent on prompt-tweak iteration.

**When to use vs skip.** Use this pattern when:
- the issue is behavioral attractor (= LLM picks the wrong path despite available alternatives)
- prior batches show the same attractor recurring across scenarios (= base rate ≥ 1)
- the root cause is unclear (= "is it the SP rule, the tool description, or the parameter schema?")
- the fix is potentially production-grade (= 1.0 release blocker, user-impact high)

Skip for: simple structural / wiring / null-safety bugs where the trace already shows the root cause, isolated bug fixes (= single file, single line), or speculative hypothesis tests with no valid evidence.

This principle pairs with principle 11 (= predict before observing) and principle batch 19 (= pre-retrospective discipline) to form a three-stage agent-self-discipline ladder: predict (prelude) → audit before retrospective (= batch 19) → audit before fix (= batch 22).

---

## 4. Common patterns and anti-patterns

### Pattern: each fix resolves one layer, revealing the next

**Abstract pattern.** In a layered system with probabilistic components (LLM, network, OS), fixing the top-visible blocker does not produce completion — it shifts the failure to the next layer. The next layer was always present but was masked by the previous blocker.

**Conceptual example.** A chain of six phases fails at phase 2 (permission denied). Fix phase 2. Now the chain fails at phase 4 (wrong artifact shape). Fix phase 4. Now the chain fails at phase 5 (LLM produces empty output). Each fix is real, each succeeds in its own terms, and each reveals a new blocker.

**Detection.** If your calibration model does not include a "blocked" category for "observation path was obstructed by a prior bug," you will systematically over-predict "verified" for scenarios that cannot even reach the relevant phase. Add "blocked" to your outcome categories and treat it as a baseline ~15–25% expectation for mid-chain phases in early batches.

**Verified fix probability by fix layer:**
- Structural fix (schema constraint, preprocessor, deterministic path): verified 40–60%
- Layer-targeted fix (correct layer, correct root cause): verified 30–45%
- Wording/prompt fix: verified 10–25%
- Wrong-layer fix (test passes, e2e fails): refuted ~80–100%

### Pattern: downstream symptom masking

**Abstract pattern.** A failing observation is classified as a root-cause bug when it is actually a symptom of an upstream failure. The upstream failure produces an anomalous intermediate result, which causes a downstream phase to fail with a different error. The downstream error is observed and treated as the primary bug.

**Detection.** Reproduce-first (Principle 6) is the primary detection mechanism. If a bug does not reproduce after an upstream fix lands, it was a downstream symptom. Document this as resolved-indirectly (not "fixed") and record the upstream cause.

**Why this matters.** Fixing a downstream symptom symptomatically (without finding the root cause) is wasteful — the symptom will recur whenever the upstream condition recurs. Prompt-based symptom fixes are especially problematic: they add bloat for a failure mode that disappears when the real fix lands, and they never disappear from the system prompt.

### Anti-pattern: prompt rule accumulation trap

Each dogfood scenario finds a failure. Each failure gets a prompt rule. After N batches, the prompt has accumulated N rules, many of which target the same underlying failure mode with subtly different wording. The rules begin to interact — one rule's wording triggers a behavior in an adjacent scenario that the original scenario didn't exercise.

Detection: count the number of MUST rules in the system prompt. If the count is growing monotonically without any consolidation pass, the accumulation trap is active. Run a structural fix audit: for every prompt rule, ask whether the intended constraint can be expressed as a schema enum or a deterministic preprocessor step. If yes, move it out of the prompt.

### Anti-pattern: over-consolidation regression

The response to accumulation is consolidation. This also fails: consolidating four individual MUST bullets into a two-paragraph block weakens the signal for weak LLMs. The LLM applies lower priority to multi-sentence prose than to crisp individual bullets.

Detection: a regression in scenario behavior immediately after a prompt consolidation commit. Specifically: a scenario that was previously handled correctly by the prompt now fails after consolidation. The fix is not to revert to the original four rules verbatim, but to restore four bullets with deduplicated wording.

### Anti-pattern: N=1 milestone promotion

Observing that a scenario completed successfully in one run is not a milestone. It is a data point. LLM-driven workflows are probabilistic; a single successful run may be a non-deterministic lucky case. Milestone status requires N≥5 runs with a minimum success rate (typically ≥60% for "working," ≥80% for "stable").

Promoting N=1 to milestone causes calibration error in the next batch: the prediction for the following batch assumes the milestone behavior is stable, but the actual behavior reverts when N is increased and the underlying blocker is found.

### Anti-pattern: non-documented behavior introduction

A fix is dispatched to handle an observed failure. The fix introduces a new mechanism not described in any specification document. The mechanism works for the observed failure but introduces asymmetric behavior (as described in Principle 9). Over several batches, multiple non-documented mechanisms accumulate.

Detection: the simplicity smell test (Principle 9) triggered by inability to describe the component simply, followed by the documented design audit (Principle 8).

---

## 5. Calibration discipline

### Why predict before observing

Prediction before observation is the mechanism that converts dogfood runs from operational verification into learnable data. Without a prediction, every observation is equally compatible with your model of the system. With a prediction, a discrepancy between prediction and observation is a signal: something in your model is wrong, and you can update it.

Calibration is the practice of making your probabilities accurate: a 60% prediction should be correct approximately 60% of the time. Calibration accuracy is measured with Brier score (lower is better). Brier score history across batches 8–14:

| Batch | Brier | Primary improvement driver |
|-------|-------|---------------------------|
| 8 | 0.96 | Baseline — no blocked category |
| 9 | 0.55 | Added blocked category; learned wrong-layer |
| 10 | 0.30 | Verify-first + reproduce-first applied |
| 11 | 0.65 | N=1 provisional milestone used as base rate |
| 12 | 0.40 | Corrected for batch 11 overestimate |
| 13 | 0.20 | Documented design audit added to prediction |
| 14 | 0.18 | Stable — full framework operational |

The lesson from batch 11 regression: using a single successful run as the basis for predictions produces overconfidence. The base rate should reflect cumulative observed success rates, not single-run outcomes.

### Four-category outcome classification

Every scenario prediction should be expressed as a probability distribution over these four outcomes:

- **verified**: the fix or feature behaves as specified in the prediction
- **inconclusive**: the observation is ambiguous — either the scenario didn't reach the relevant phase, or the result is mixed across multiple sub-steps
- **refuted**: the behavior contradicts the prediction — the fix had no effect, or had the wrong effect
- **blocked**: the observation path was obstructed by a prior bug — the scenario could not reach the relevant phase

The blocked category deserves emphasis: it is the most commonly omitted category for new dogfooders. In layered systems with multiple blockers, "blocked" is the most likely outcome for mid-chain scenarios in early batches. Omitting it forces all those outcomes into "inconclusive," which inflates the inconclusive rate and makes the prediction model useless for that category.

### Base rates by fix type

These are historically observed success rates from batches 7–14. Treat as rough priors, not guarantees:

| Fix type | Verified rate | Notes |
|----------|--------------|-------|
| Structural (schema / enum) | 40–60% | Deterministic effect; remaining failures are usually next-layer |
| Deterministic path fix (preprocessor) | 40–60% | Same as structural |
| Layer-targeted bug fix (correct diagnosis) | 30–50% | May still hit wrong layer |
| Wording-only prompt fix | 10–25% | Weak LLMs often don't honor wording changes |
| Wrong-layer fix | ~0% (refuted) | Test passes, e2e fails |

### N≥5 stability requirement

A behavior change observed in N=1 is not stable until confirmed in N≥5 runs. The minimum threshold for a "working" declaration is N≥5 with ≥60% success. For a "stable" (production-ready) declaration, the threshold is N≥5 with ≥80%.

The reason for the N≥5 requirement: LLM-driven workflows have non-deterministic failure modes that may not manifest on every run. A single successful run is compatible with both "fixed" and "fixed on this particular sequence of LLM decisions, but not in general."

### N≥10 for "deterministic" attractor claims (= post-OSS reinforcement)

A separate threshold applies to the **opposite** claim: declaring an attractor or empty-stop pattern "deterministic" requires N≥10. This rule was added in 2026-05-07 after a methodological mistake: an analyst saw 0/5 empty-stops across 11 different patches and concluded "deterministic gemini quirk", then built increasingly complex hypothesis stacks (= LiteLLM translation bug, vendor-side defect, ADR-0021 boundary discussion). N=20 measurement on the same payload revealed the actual rate was ~85% narrate / ~15% empty stop — the "0/5 across 11 patches" was a streak artifact of running 5-shot batches when the underlying empty-stop rate happened to align with rapid-succession sampling.

Rules:

- For **rate measurement** (= "what fraction succeeds?"): N≥10 minimum, N=20 preferred when the claim affects design decisions.
- Do not use N=5 to declare a behavior "always fails" or "always succeeds" — N=5 with a true 80% rate has a 0.2^5 ≈ 0.03% false-streak probability per run, but if you run that 11 times you have an 11 × 0.03% ≈ 0.35% chance of a streak somewhere. Across many hypothesis tests the streak probability compounds.
- When tempted to invoke "vendor X has a public-unreported defect" or "library Y has a translation bug": apply Occam's razor first. The simpler hypothesis — "my testing methodology produced a streak that I'm misreading as deterministic" — is almost always more likely than the complex hypothesis. Verify the simpler one first by increasing N before chasing the complex one.

cross-ref: `feedback_minimize_speculation.md`, `feedback_observe_before_speculate_llm.md`. The G12 entry in `docs/deep-dives/journal/dogfood/giveup-tracker.md` documents the specific case (= Pattern E observation, post-OSS HN dogfood 2026-05-07).

---

## 6. Reyn-specific tooling

> **This section is Reyn-specific.** The principles in this section (observation infrastructure, payload inspection, replay) apply to all LLM-driven systems. The specific tools described here are Reyn's implementation of those principles. If you are adapting this discipline to a different system, see the "Adapting to other systems" paragraph at the end of this section.

### Why these tools exist

Before batch 7, LLM behavior analysis at Reyn was conducted without any mechanism to observe what the LLM actually received. Hypotheses about the LLM's behavior were formed by reading code. This produced a five-deep speculation stack that took multiple batches to unwind and cost several wrong-layer fixes.

The batch 7 observation infrastructure investment changed the iteration speed from "days per hypothesis" to "minutes per hypothesis." The toolkit covers: full payload capture, payload inspection, payload replay, attractor auto-detection, and a script-friendly chat surface for non-TTY exercise.

### REYN_LLM_TRACE_DUMP

Set the environment variable `REYN_LLM_TRACE_DUMP=<path>` before running `reyn chat` or `reyn run`. Reyn writes every LLM call's full input payload — system prompt, messages, tools schema — to a JSONL file at `<path>`.

This file is the ground truth for every LLM behavior question. "Did the model see the enum constraint?" — read the tools schema in the dump. "Is the prompt rule present?" — read the system prompt in the dump. "What did the model return?" — read the response.

The dump is **production-gated** (disabled by default in production deployments) and should be used in dogfood sessions and debug sessions only.

### scripts/dogfood_trace.py

A multi-mode inspection utility for dump files and workspace state:

```bash
# Inspect LLM payload summaries
python scripts/dogfood_trace.py --trace <path.jsonl> --mode llm-payloads

# Show full system prompt + messages for one call
python scripts/dogfood_trace.py --trace <path.jsonl> --mode llm-detail --call-id <id>

# Inspect the tools schema for a call
python scripts/dogfood_trace.py --trace <path.jsonl> --mode llm-tools-schema --call-id <id>

# Multi-trace merge for cross-session comparison
python scripts/dogfood_trace.py --trace a.jsonl,b.jsonl --mode llm-payloads
```

This tool replaces the pattern of manually running `grep`, `jq`, and `cat` on raw JSONL files. For a batch with 4–5 scenarios and multiple LLM calls per scenario, the manual approach costs 10+ tool calls per scenario; `dogfood_trace.py` collapses it to one.

### scripts/llm_replay.py

Replays a captured LLM call directly via LiteLLM, bypassing Reyn's OS layer. This is the primary tool for hypothesis testing:

```bash
# Replay a captured call
python scripts/llm_replay.py --trace <path.jsonl> --call-id <id>

# Replay with a patched payload (e.g., modify the system prompt)
python scripts/llm_replay.py --trace <path.jsonl> --call-id <id> --patch '{"system": "..."}'

# Show diff between original response and replayed response
python scripts/llm_replay.py --trace <path.jsonl> --call-id <id> --diff

# Run N times to measure probability distribution
python scripts/llm_replay.py --trace <path.jsonl> --call-id <id> --n 10

# Replay with a different model (model spike)
python scripts/llm_replay.py --trace <path.jsonl> --call-id <id> --model openai/gpt-4o
```

The `--patch` flag is what enables **pre-landing fix verification**: you can modify the payload to reflect a proposed fix (e.g., add an enum field, change a prompt rule wording) and observe the LLM's response before touching any code. This collapses the test cycle from "implement fix → run dogfood → observe" to "patch payload → observe."

The `--n` flag enables **probability distribution measurement**: run the same payload 10 times and count how many times the LLM produces each distinct output. This is how deterministic vs. probabilistic failures are distinguished, and how attractor fix effectiveness is measured.

### scripts/detect_attractor.py

Automated detection of three attractor patterns in a dogfood workspace:

1. **Empty stop**: the LLM produced a `finish` output with empty content
2. **Enum violation**: the LLM chose an option not in the enum constraint
3. **Tool name hallucination**: the LLM called a tool by a name not in the tools schema

```bash
python scripts/detect_attractor.py --trace <jsonl_path>
```

Run this after every dogfood batch to catch attractor patterns that might not be visible in the high-level scenario outcome. A scenario can "complete" (produce a final output) while containing one or more attractor events at intermediate phases.

### scripts/hn_research.py

Industry-research tool: runs a site-scoped DuckDuckGo search for a topic on `news.ycombinator.com`, fetches full thread JSON from the Algolia HN API, and prints a digest of top posts with their top comments. Use for repeatable positioning / design research waves (see `docs/deep-dives/journal/insights/2026-05-09-hn-ai-agent-landscape-insights.md` for the motivating example).

```bash
python scripts/hn_research.py --topic "AI agent" --max-results 10 --top-comments 5
python scripts/hn_research.py --ids 47733217,48035677 --top-comments 3
```

### `reyn web` A2A endpoint — script-friendly chat exercise

The TUI is not the only way to drive Reyn. `reyn web` starts a FastAPI server on `localhost:8080` that exposes every registered agent as an A2A (Agent2Agent) JSON-RPC endpoint. This is the right surface for:

- **Scripted reproduction of a chat flow** during fix verification — `curl` from a shell loop is much easier than scripting the TUI.
- **Sanity-checking a tutorial example query** from a non-TTY environment (CI, agent harness, this very session).
- **Exercising a specific agent without `--attach` ceremony** — every agent is addressable by name in the URL.
- **Driving Reyn from another LLM** (Claude Code, Cursor) when MCP isn't set up but HTTP is.

**Start the server:**

```bash
reyn web --reload         # binds 127.0.0.1:8080, auto-reloads on code edit
reyn web --port 9000      # override port
reyn web                  # plain mode — does NOT reload on edit
```

**Use `--reload` for dev/debug iteration.** Without it, edits to tool descriptions, system prompts, or any router code stay invisible until you manually `kill` the process and re-`reyn web`. With `--reload`, uvicorn picks up file changes within ~2 s. The dogfood feedback loop (edit `router_tools.py` → re-curl the A2A endpoint) becomes hands-free.

The server reads the same `reyn.yaml` and registry as `reyn chat` — no separate config.

**List the agents (server-level discovery):**

```bash
curl -s http://localhost:8080/a2a/agents | jq
```

Returns every registered agent (`default`, anything you created with `reyn agent new`, plus any `_default` topology auto-creates).

**Send a message and read the reply (single round-trip):**

```bash
curl -s -X POST http://localhost:8080/a2a/agents/default \
  -H 'Content-Type: application/json' \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "message/send",
    "params": {
      "message": {
        "kind": "message",
        "role": "user",
        "messageId": "t1",
        "parts": [{"kind": "text", "text": "what is this project about?"}]
      }
    }
  }' | jq -r '.result.parts[0].text'
```

The reply is the agent's final synthesised text — exactly what the TUI would render. Multi-turn history persists across calls within the same agent, so a follow-up `POST` on the same agent continues the conversation.

**When to reach for `dogfood_trace.py` / `llm_replay.py` instead.** The A2A endpoint exercises the full chat path including routing, skill spawn, and multi-turn synthesis. If you only want to inspect or replay the LLM payload of a single phase, the trace/replay tools are more surgical. Use A2A when the question is "what does the user see end-to-end"; use trace/replay when the question is "what did the LLM see, and what does it produce on a different prompt."

**Why this is easy to forget.** The web server isn't part of the dogfood batch driver scripts (those drive `reyn chat --cui` via subprocess for parity with real users). The A2A endpoint is the operator's hand-driven debug tool; reach for it when piping into `reyn chat --cui` is awkward (TUI buffering issues, missing terminal, etc.).

### scripts/dogfood_sp_render.py

A CLI for verifying system prompt rendering. Preview the wrapper-only or legacy SP and get size-delta stats in one command, without writing a throwaway script each time you want to confirm what the LLM will receive.

Full reference: [docs/reference/dogfood-sp-render.md](../../reference/dogfood-sp-render.md)

### Adapting to other LLM-driven systems

The core requirement is payload observability: you must be able to see what the LLM receives and produces for each call. Every LLM API provider supports capturing request/response pairs; the question is whether your system routes all calls through a capture layer.

The minimum viable observation stack:
1. A capture mechanism that writes `{call_id, system_prompt, messages, tools, response}` to a structured log for every LLM call
2. An inspection utility that can filter and display that log by call ID and field
3. A replay mechanism that can re-run a captured payload with modifications

Reyn's three tools (`REYN_LLM_TRACE_DUMP`, `dogfood_trace.py`, `llm_replay.py`) are one implementation. Any LLM proxy layer (LiteLLM proxy, custom middleware) can implement the same three capabilities. The attractor detector is a post-processing step that can be rebuilt for any domain given the captured payloads.

---

## 6.5 Plan-mode dogfood specifics

> **This section applies once plan-mode is in your test scope.** The nine principles in Section 3 still apply without modification. What changes is the observation axis: plan-mode introduces async dispatch, concurrent in-flight plans, and memo replay on resume — surfaces that skill-side dogfood never exercises.

---

### 6.5.1 Why plan-mode needs special discipline

Skill-side dogfood verifies that a skill's phase graph executes correctly under the LLM's probabilistic decisions. Plan-mode adds three qualitatively different concerns:

**Async dispatch and completion ordering.** A plan runs as a background `asyncio.Task`. The user can issue new messages while the plan is in flight. Multiple plans can overlap. Outbox messages land in completion order, not user-issue order. These properties are invisible in skill-side traces — they only surface when you deliberately run concurrent plans and observe the outbox.

**Memo replay on resume.** The value of crash resilience (ADR-0023 + ADR-0025) is not testable by reading code. It requires killing a process mid-step, restarting, and confirming that completed steps produce zero additional LLM cost and identical outputs. This is a distinct observation path from any skill-side test.

**Router-side LLM invocation.** Plan-mode is triggered by the chat router LLM choosing the `plan` tool. Unlike skill-side dogfood — where the user controls which skill is invoked — plan-mode depends on the router LLM deciding, probabilistically, that decomposition is warranted. A scenario that is "complex enough" by human judgment may still not trigger plan-mode if the router LLM consistently prefers direct answers. This makes plan invocation itself an observation point, not an assumption.

The principles in Section 3 still apply — in particular Principle 4 (build observation infrastructure first), Principle 6 (verify-first / reproduce-first), and Principle 3 (one hypothesis, one fix) — but the specific observation surfaces differ from skill-side dogfood.

---

### 6.5.2 New observation surfaces

Plan-mode produces durable state across six distinct locations. Each has a different purpose and a different decay lifecycle.

| Surface | Where | What to look for |
|---|---|---|
| WAL | `state/wal.jsonl` | `plan_started` / `plan_completed` / `plan_aborted` / `plan_step_started` / `plan_step_completed` / `plan_step_failed` — the resume substrate |
| Events log (forensic-only) | `events/<caller>/...` | `plan_emitted` / `plan_aggregated` / `plan_run_interrupted` / `plan_step_memoized` / `plan_step_memo_failed` / `plan_step_llm_memoized` |
| Per-plan snapshot | `state/plans/<plan_id>.snapshot.json` | `step_results` / `step_result_refs` / `step_llm_calls` — the durable cache that drives resume |
| Spilled step results | `state/plans/<plan_id>/step_results/<step_id>.txt` | ADR-0024 — only outputs > 32 KB spill; inline otherwise |
| Spilled LLM call records | `state/plans/<plan_id>/step_llm_calls/<step_id>/<turn_idx>.json` | ADR-0025 — only > 32 KB results spill |
| Outbox (= UI / TUI) | `session.outbox` queue, also visible in chat REPL | `kind=status` per-step progress narration; `kind=agent` terminal text with `meta.plan_id` |
| Running tasks | `session.running_plans: dict[plan_id, asyncio.Task]` | shown via `/plan list` slash command |

**Reading discipline for each surface:**

- **WAL first.** The WAL is the primary resume substrate and the fastest surface to read. If a step is claimed to have completed, `plan_step_completed` must be present. If it is not, the step never committed — the snapshot may contain stale data.
- **Snapshot second.** The snapshot is what the resume coordinator reads. Confirm `step_results` (inline) or `step_result_refs` (spilled) are populated for the steps you expect to be memoized.
- **Events log for causality.** `plan_step_memoized` and `plan_step_llm_memoized` confirm that the memo path fired, not just that the result is present. Use the events log only for this forensic use — not for operational checks.
- **Outbox for user-facing correctness.** The outbox is what the user sees. `meta.plan_id` tagging is what distinguishes concurrent plan outputs. Verify that each plan's terminal text carries the right `plan_id`.

---

### 6.5.3 Tooling cheat sheet

The `dogfood_trace.py` utility exposes plan-specific modes alongside the existing skill-side modes.

```bash
# Plan-mode summary (= count plans, memo hits, max concurrent)
python scripts/dogfood_trace.py --mode plan-summary

# Per-plan timeline (= WAL + events log + outbox for one plan_id)
python scripts/dogfood_trace.py --mode plan-trace <plan_id>

# Per-plan workspace dump (= decomposition + snapshot + spilled files)
python scripts/dogfood_trace.py --mode plan-snapshot <plan_id>

# Cost mode now includes memo savings estimate
python scripts/dogfood_trace.py --mode cost
```

> Note: `--mode plan-summary`, `plan-trace`, and `plan-snapshot` are being added in the same prep wave as this section. If not yet landed, treat this as forward-looking documentation. Write scenarios that exercise the surfaces above using manual WAL / snapshot inspection until the modes land.

For the existing skill-side modes, see Section 6. The `--mode cost` output is shared — it includes both skill-side and plan-side LLM call costs, with memo savings broken out separately when plan resumptions have fired.

The attractor detector (`scripts/detect_attractor.py`) is useful for plan-mode too: run it against the sub-loop's trace dump to catch empty-stop or enum violation attractors inside individual step executions.

```bash
REYN_LLM_TRACE_DUMP=plan_trace.jsonl reyn chat
python scripts/detect_attractor.py --trace plan_trace.jsonl  # catches step-level attractors
```

---

### 6.5.4 Plan-mode-specific scenario design

For batch design (A1 step), plan-mode requires deliberately constructed scenarios that exercise its distinct properties. Five scenario classes cover the main risk surface:

#### Class 1: Multi-source synthesis (long-step)

**Purpose.** Verify that the router LLM actually invokes plan-mode for a query that warrants it — and that the decomposition and aggregation are coherent.

**Example query.** "Compare the README and CLAUDE.md and summarize the key differences for a new contributor."

**What to observe.**
1. Does the router LLM call the `plan` tool? (Check `REYN_LLM_TRACE_DUMP` for a `plan` tool call in the router's turn.)
2. Is the decomposition well-formed (2–7 steps, no circular dependencies)?
3. Does the terminal aggregator step produce a coherent answer that cites both sources?

**Verified.** Router calls `plan`; decomposition is present at `state/plans/<plan_id>/decomposition.json`; outbox receives a `kind=agent` message with `meta.plan_id`; content references both documents.

**Refuted.** Router answers directly without calling `plan`. This is not a bug (the router may legitimately decide direct answer is better), but it means your scenario is not testing plan-mode. Revise the query to be more explicitly multi-source.

**Blocked.** Router errors before producing any tool call. Treat as a prior-layer bug, not a plan-mode finding.

#### Class 2: Concurrent plans (multi-plan UX)

**Purpose.** Verify that multiple in-flight plans produce correctly tagged outbox output in completion order, not issue order.

**Execution.** Issue two user prompts back-to-back (before either plan completes) — one short plan (2 steps), one longer plan (5 steps). Observe outbox ordering.

**What to observe.**
1. Does the outbox receive two separate `meta.plan_id`-tagged final messages?
2. Does the shorter plan's message arrive before the longer plan's — regardless of which was issued first?
3. Does `/plan list` show both plans as active before either completes?

**Verified.** Two distinct `plan_id` values; shorter plan's `kind=agent` message arrives first; both plans complete without state collision.

**Refuted.** Plans complete in issue order regardless of duration — suggests outbox ordering is incorrect. Or: state collision (one plan's step results appear in the other's snapshot).

**Blocked.** Router only triggers plan-mode for one of the two queries.

#### Class 3: Crash + resume

**Purpose.** Verify ADR-0023 (step-result memoization) and ADR-0025 (sub-loop LLM call memoization) fire on resume. This is the most important scenario class for crash resilience confidence.

**Execution.**
1. Start a multi-step plan (Class 1 or longer).
2. Wait for step 1 to emit `plan_step_completed` in the WAL.
3. `kill -9` the `reyn chat` process mid-step-2.
4. Restart `reyn chat`.
5. Observe resume behavior.

**What to observe.** (See 6.5.5 for full procedure.)

**Verified.** Step 1 does not incur new LLM cost on resume (`plan_step_memoized` event fires); step 2 re-executes from its interrupted point; the plan completes correctly.

**Refuted.** Step 1 re-incurs LLM cost on resume (no `plan_step_memoized` event, new entries in cost ledger for step 1's calls).

**Blocked.** `_recover_plans_for_agent` does not fire (log message absent) — suggests the WAL replay or agent registry restore path has a bug upstream of plan-mode.

#### Class 4: Operator escape hatch

**Purpose.** Verify `/plan list`, `/plan discard <plan_id>`, and `/plan resume <plan_id> --from <step_id>` operate correctly on live and interrupted plans.

**What to observe.**
1. `/plan list` — shows correct `plan_id`, step counts, and running/pending state during an in-flight plan.
2. `/plan discard` — cancels the task, writes `plan_aborted` to WAL, removes decomposition artifact and snapshot, sends outbox notice.
3. `/plan resume --from <step_id>` — re-executes from the specified step; earlier steps memo-replay; final output reflects the re-run step.

**Verified.** Each command produces the expected WAL entry and outbox state change.

**Refuted.** `/plan discard` does not remove the decomposition artifact — risk of stale artifact ghost (see 6.5.6 anti-pattern).

#### Class 5: Long-tail step (> 32 KB output)

**Purpose.** Verify ADR-0024 step-result spill triggers without data loss when a step produces output exceeding 32 KB.

**Execution.** Construct a step that synthesizes a large text output (e.g., "list all symbols exported by every file in `src/`" — typically > 32 KB for a medium-size codebase).

**What to observe.**
1. Does `step_result_refs.<step_id>` appear in the snapshot (rather than `step_results.<step_id>`)?
2. Does `state/plans/<plan_id>/step_results/<step_id>.txt` exist and contain the full output without truncation?
3. Does the downstream aggregator step receive the full content (= transparent resolution via `get_step_result`)?

**Verified.** `step_result_refs` populated; spilled file exists; downstream step content references content only present in the spilled file (= no truncation).

**Refuted.** Output appears inline in snapshot despite being > 32 KB — spill did not trigger. Or: spilled file exists but downstream step received a truncated version.

---

### 6.5.5 Memo hit verification procedure

This is the step-by-step procedure for confirming that both ADR-0023 (step-result memoization) and ADR-0025 (sub-loop LLM call memoization) replay correctly on resume. Run this procedure when executing a Class 3 scenario.

**Step 1: Run a plan to completion (baseline).**

Start with a clean state directory (`state/plans/` empty or containing no active plans). Run a multi-step plan (3+ steps recommended) and allow it to complete cleanly. Record:
- The `plan_id` from the WAL or `/plan list`.
- The cost ledger output from `python scripts/dogfood_trace.py --mode cost` (captures fresh-run LLM cost for comparison).

**Step 2: Run the same query; kill mid-step-2.**

Re-run the same query. This starts a new plan with a new `plan_id`. Watch the WAL for `plan_step_completed` for step 1 (`s1`). Once it appears, `kill -9` the process immediately. Step 2 (`s2`) should be in progress or not yet started.

**Step 3: Restart `reyn chat`.**

The resume path triggers automatically on startup. Observe the log output for:
```
_recover_plans_for_agent fired for agent <name>, plan_id <id>
```
If this message is absent, the WAL replay or agent registry restore path has a problem upstream of plan-mode — file a prior-layer bug.

**Step 4: Open the per-plan snapshot.**

```bash
cat state/plans/<plan_id>.snapshot.json | python -m json.tool
```

Confirm:
- `step_results.s1` (inline) **or** `step_result_refs.s1` (spilled) is populated with the result from step 1's first run.
- `step_llm_calls.s1` is populated with the sub-loop's recorded LLM call entries.

If either is absent, the snapshot did not commit before the kill — the kill timing was too early. Re-try with a longer step.

**Step 5: Watch for `plan_step_memoized` in events log.**

After restart, observe the events log for the plan:

```bash
python scripts/dogfood_trace.py --mode plan-trace <plan_id>
```

Confirm `plan_step_memoized` appears for `s1` (not `plan_step_completed`). The distinction:
- `plan_step_completed` = step executed fresh.
- `plan_step_memoized` = step was replayed from snapshot without LLM calls.

If `plan_step_completed` appears for `s1` instead, memo replay did not fire — step 1 re-executed, incurring fresh LLM cost.

**Step 6: Watch for `plan_step_llm_memoized` for sub-loop calls within s1.**

If step 1 involved multiple sub-loop turns (= multiple LLM calls within the step executor), each sub-loop LLM call that was recorded before the kill should emit `plan_step_llm_memoized` on resume. This is the ADR-0025 mechanism — it prevents re-paying sub-loop LLM cost even when a step was only partially completed.

**Step 7: Confirm no additional LLM cost for s1.**

Run `python scripts/dogfood_trace.py --mode cost` after the resume completes. The cost ledger for the resumed plan should show:
- Step 1 (`s1`): $0.00 (or 0 tokens) — memo hit.
- Step 2+ (`s2`...): fresh cost — these re-executed.

If `s1` shows non-zero cost, memoization did not fire for step 1. This is a HIGH bug: the crash resilience claim is not upheld.

**Step 8: Verify plan completes correctly.**

The resumed plan should complete with the same terminal output as the baseline run (Step 1). If the output differs materially (not just whitespace / token sampling variance), the memo replay introduced data corruption. File as CRITICAL.

---

### 6.5.6 Common patterns / anti-patterns specific to plan-mode

This section extends Section 4's patterns and anti-patterns to plan-mode-specific cases. See Section 4 for the skill-side layer-by-layer and downstream symptom patterns, which apply equally here.

#### Pattern: multi-plan completion order is correct by design, not coincidence

When two plans are in flight and the shorter one completes first, the outbox ordering is **correct behavior** — not a timing coincidence. The `meta.plan_id` tag on each `kind=agent` message is the mechanism for the UI to attribute output to the correct plan even when ordering differs from issue order.

Implication for scenario design: when running Class 2 (concurrent plans), explicitly verify the `meta.plan_id` values in the outbox messages. Do not rely on position alone to determine which plan produced which output. A UI that shows plan outputs in a mixed order without `meta.plan_id` attribution is a UX bug, not a plan-mode bug.

#### Pattern: spill-vs-inline crossing the 32 KB threshold mid-run is normal

Whether a step result lands inline in the snapshot or spills to a file is determined by the step's output size at the time it is written. Both paths are correct. A test batch in which some steps spill and others do not is not a sign of inconsistency — it reflects the actual output size distribution of the scenarios.

Do not add special-case assertions for "this step must spill" unless you have constructed the scenario specifically to produce > 32 KB output (Class 5). For general-purpose scenarios, treat both as valid outcomes and verify only that the downstream step received the correct content regardless of path.

#### Anti-pattern: treating `plan_step_failed` as a hard error

Per-step failures are caught and recorded by the plan runtime. The plan continues to execute subsequent steps (unless the failed step's output is required by a downstream step). A dogfood finding that surfaces `plan_step_failed` in the WAL is **not automatically a HIGH bug** — it depends on whether:
1. The failure was expected (the step's query had no valid answer).
2. The downstream steps gracefully handled the missing input.
3. The final aggregator produced a coherent output despite the failure.

When `plan_step_failed` appears, verify graceful degradation before escalating severity. If the plan completes with a coherent output that acknowledges the failure, severity is MED (degraded, not broken). If the plan silently produces an incorrect aggregated output without surfacing the failure, severity is HIGH (data correctness issue).

Cross-ref: this is the plan-mode analog of "downstream symptom masking" in Section 4 — a visible failure event is not always the root-cause finding.

#### Anti-pattern: re-running with a stale decomposition artifact

If `state/plans/<plan_id>/decomposition.json` lingers from a previous run (e.g., after a `/plan discard` that did not complete cleanly, or after a manual kill before the artifact was removed), the resume coordinator will attempt to replay the old plan shape for the new run's `plan_id`. The result is unpredictable: step IDs may not match, memoization may fire for the wrong steps, or the coordinator may discard the plan entirely with a corrupt-artifact notice.

**Between batches that exercise fresh-start scenarios, clean `state/plans/` completely:**

```bash
rm -rf .reyn/state/plans/
```

This is mandatory before running Class 3 (crash + resume) or Class 5 (long-tail) scenarios if any prior interrupted run left artifacts behind. It is safe to run between batches — the WAL will not reference the removed artifacts after cleanup.

#### Per-scenario wipe recipe (= what every worker must reset between scenarios)

The full reset every dogfood worker should run **between scenarios within a batch** (= not just between batches) is:

```bash
rm -rf .reyn/events
rm -rf .reyn/agents/<worker-agent-name>/events
rm -f  .reyn/state/action_usage.jsonl
rm -rf .reyn/state/plans/
rm -rf reyn/local/                       # ← B30-NEW-3 addition
```

The `reyn/local/` line was added in B30 follow-up: `skill_builder` writes persistent skill files there, and on subsequent scenarios the LLM sees those skills in `list_actions` enumeration — silently contaminating the catalog. Observation: B30 worker 1 had S6's `list_comprehension_generator` skill bleed into S3's skill list because `reyn/local/` was not reset.

Note: `reyn/local/` is the **workspace-local skill directory** (= `Skill resolution order` step 2 in CLAUDE.md). It is gitignored by default and lives under cwd, so a worktree-isolated worker cleaning its cwd's `reyn/local/` does not affect any other worker.

---

### 6.5.7 Calibration adjustments for plan-mode

Section 5 establishes the four-category outcome classification (verified / inconclusive / refuted / blocked) and the general base rates. For plan-mode batches, add three per-scenario binary predictions before executing each batch:

**Binary prediction 1: "Memo will fire on resume" (Class 3 scenario)**

This is a testable binary claim. Express it as: "Given a kill-9 mid-step-2 after step-1 completion, `plan_step_memoized` will appear for step 1 on resume."

Suggested prior for first plan-mode batch: 60% verified (= the mechanism exists but the kill timing may miss the commit window, causing blocked). Calibrate from there.

**Binary prediction 2: "Multi-plan completion order matches duration order, not issue order" (Class 2 scenario)**

Express as: "The shorter-duration plan's `kind=agent` message will appear in the outbox before the longer-duration plan's."

Suggested prior: 70% verified (= the design guarantees this, but concurrent LLM timing may produce near-simultaneous completions where the order is ambiguous within a narrow window). The "inconclusive" outcome is when both plans complete within one second of each other.

**Binary prediction 3: "32 KB spill triggers without manual intervention" (Class 5 scenario)**

Express as: "A step producing > 32 KB output will emit `step_result_refs` in the snapshot, and the spilled file will exist at `state/plans/<plan_id>/step_results/<step_id>.txt`."

Suggested prior: 75% verified (= deterministic threshold, but constructing a step that reliably produces > 32 KB on a given scenario requires knowing the LLM's output verbosity, which varies).

**Brier scoring plan-mode predictions.**

Score these three binaries per batch using the same Brier formula as Section 5. Track them separately from the skill-side predictions — plan-mode and skill-side have different base rate profiles and should not be pooled until you have enough data to confirm they behave similarly.

Expected Brier score trajectory for plan-mode batches (rough prior based on structural analogy with skill-side batches 7–9):
- Batch 1 (plan-mode): 0.7–0.9 (observation surfaces unfamiliar, kill timing unreliable)
- Batch 2–3 (plan-mode): 0.3–0.5 (observation surfaces learned, kill timing practiced)
- Batch 4+ (plan-mode): 0.2–0.3 if the nine principles are applied consistently

---

### 6.5.8 What NOT to do (scope discipline)

Plan-mode is a **chat-router-side feature**. Its scope is the dispatch, execution, memoization, and resume of plans within a single agent's runtime. Do not conflate with skill-side dogfood or with multi-agent coordination.

**DO NOT test sub-loop tool-op memoization expectations.**

ADR-0023 §3.4 explicitly defers tool-op (= non-LLM tool dispatch) memoization from the plan resume design. When a plan step resumes, its sub-loop's tool dispatches (e.g., file reads, workspace writes) **re-execute** — this is the documented design, not a bug. A dogfood finding that observes "file read was repeated on resume" should be classified as `verified` (correct behavior), not as a bug.

Testing this expectation falls outside plan-mode dogfood scope. If you want to verify tool-op idempotency under re-execution, that belongs in a separate skill-side scenario targeting the specific tool op.

**DO NOT expect plans to share state across user turns.**

A plan's scope is the single user turn that triggered it (unless explicitly resumed via `/plan resume`). State written by one plan's steps is not automatically available to a subsequent plan triggered by the next user turn. Each plan has its own `plan_id`, its own snapshot, and its own decomposition artifact.

If you observe state appearing to carry over between turns, investigate whether a workspace file (= P5 SSoT) was written by one plan and read by another — this is correct and expected. If plan-internal state (snapshot, decomposition) appears shared, that is a bug.

**DO observe whether plan-mode is invoked by the LLM at all.**

This is the most common plan-mode dogfood miss. If the router LLM never calls the `plan` tool, your scenario is not testing plan-mode regardless of how complex the query is. Before analyzing any plan-mode-specific finding, confirm in `REYN_LLM_TRACE_DUMP` that the router's turn contains a `plan` tool call. If it does not, the scenario is a skill-side routing scenario, not a plan-mode scenario.

If you consistently cannot trigger plan-mode across multiple query formulations, apply Principle 4 (observation infrastructure) before forming hypotheses: dump the router's system prompt and tool schema, confirm the `plan` tool is present, and inspect what the router received. The router may not have the tool in its catalog for a given session configuration.

---

## 6.6 Long-lived session pattern (G12 / context-bloat measurement)

### A. Why this pattern exists

The existing per-run dogfood pattern (described in Section 2 and throughout Section 6) resets workspace state between every scenario execution. This isolation is valuable for measuring R1-type attractors — cases where the LLM refuses, misroutes, or produces a structurally invalid output on a fresh context. But it cannot measure G12-type attractors, specifically Pattern E: empty completions that are triggered by context bloat across multiple turns. G28 (see `giveup-tracker.md`) made this measurement gap explicit: batch 16 observed an 8% empty-reply rate that was later traced not to a production issue but to the dogfood driver's `clean_state` call invalidating disk history out of step with the server's in-memory `ChatSession._history`. The result was artificial context duplication that production users never experience. To measure actual production-equivalent behavior — where a user's session grows naturally across turns without any reset — the driver must mirror that lifecycle.

### B. The driver

`scripts/dogfood_long_session.py` is a long-lived session driver for Reyn. It sends prompts in order to the same A2A agent endpoint, allowing history to accumulate naturally across turns, and harvests per-turn metrics and the events log at the end of each scenario.

**What it records (per turn):**

- `reply_len`: character count of the synthesised text reply
- `elapsed_s`: wall-clock latency for the turn
- `empty`: whether the reply was empty (zero non-whitespace characters)
- HTTP status and any JSON-RPC error message

**Post-scenario harvest:**

- Budget-ledger token entries for the agent (total tokens and LLM call count)
- Events log path (for downstream attractor analysis with `detect_attractor.py`)

**What it does NOT reset between turns:** history. The server-side `ChatSession._history` grows continuously across all turns of a scenario, exactly as it does for a production user.

**Scenarios file:** `dogfood/scenarios/long_session_v1.yaml` — 7 sample scenarios covering research chains, pronoun-reference followup, cross-reference comparison, repetitive context (the primary G12 Pattern E target), general Python topics, file/doc lookup chains, and concept explanation chains.

**CLI invocation used for the baseline:**

```bash
python scripts/dogfood_long_session.py \
    --scenarios dogfood/scenarios/long_session_v1.yaml
```

Additional flags:

```bash
# Target a different agent or port
python scripts/dogfood_long_session.py \
    --scenarios dogfood/scenarios/long_session_v1.yaml \
    --agent default --web-url http://localhost:8080

# Multi-shot (each shot uses a distinct agent endpoint: default-shot1, default-shot2, ...)
python scripts/dogfood_long_session.py \
    --scenarios dogfood/scenarios/long_session_v1.yaml \
    --n-shot 3

# Emit structured JSON for downstream analysis
python scripts/dogfood_long_session.py \
    --scenarios dogfood/scenarios/long_session_v1.yaml \
    --json --out results.json
```

For `--n-shot N > 1`, each shot uses a distinct agent name (`default-shot1` through `default-shotN`) so each shot gets a truly fresh server-side session. The agents must exist in the registry or be pre-created with `reyn agent new`.

### C. When to reach for which pattern

| Question to answer | Use this driver |
|---|---|
| "What is the LLM's R1 refusal rate for a given scenario?" | Per-run clean_state (existing pattern, Section 2–5) |
| "What is the empty-completion base rate over a multi-turn conversation?" | Long-lived session (this section) |
| "Does my new fix change context-handling across turns?" | Long-lived session — re-run scenarios pre/post fix |
| "How does the agent behave in plan-mode crash + resume?" | Per-run clean_state with the `kill -9` procedure (Section 6.5 Class 3) |
| "Is a specific attractor present in a single-turn scenario?" | Per-run clean_state + `detect_attractor.py` |

### D. Known limitations

- **Empty detection operates at the response-text layer, not the events layer.** The driver counts a turn as empty when the synthesised `reply_len` is zero. This captures the user-visible empty reply. It does not directly correlate with `finish_reason: stop` + `completion_tokens: 0` in the events log. If you need to verify whether an empty turn was caused by G12 Pattern E vs. a JSON-RPC error vs. a network timeout, consult the events log and the `status` field in the JSON output.

- **Token growth curve by turn position is not directly available.** The budget ledger records total tokens per agent session, not per turn. The `--json` output includes raw `token_entries` with timestamps; downstream per-turn correlation requires matching budget-ledger timestamps against turn wall-clock timestamps. For coarse analysis, per-scenario total tokens are sufficient.

- **N=37 turns is small for stable rate estimates.** The baseline run (7 scenarios × 5–6 turns) produced a 2% overall empty rate. This is a useful directional signal but the margin of error at N=37 is large. For a rate estimate with ±5% precision at 95% confidence, N ≥ 100 turns are needed. Run multiple shots (`--n-shot N`) or expand the scenario set to increase N before drawing hard conclusions.

- **Context bloat at very long turn counts (10+, 20+) was not measured.** The baseline used 5–6 turns per scenario. G12 Pattern E manifests as a context-size function. Whether empty completions increase at 10+ turns is an open question — scenarios would need to be extended or new ones added to test that range.

### E. Cross-references

- **Section 5 calibration discipline** (N≥10 / N≥5 requirement): the long-session pattern is one half of the measurement picture; per-run clean_state is the other half. Neither alone is sufficient.
- **G28 in `giveup-tracker.md`**: the entry that motivated this driver, including the batch 16 8% baseline and the confirmed driver-induced explanation.
- **`dogfood/scenarios/long_session_v1.yaml`**: the 7-scenario starting set. Extend it when testing specific context-growth hypotheses.
- **`scripts/detect_attractor.py`**: run against the events log path reported by `--json` to check for empty-stop events at the phase level.

---

## 7. Quickstart for new dogfooders

### Starting a new batch — checklist

Before writing any scenario:

- [ ] Read the most recent batch's retrospective to understand carry-over findings
- [ ] Identify which HIGH and CRITICAL findings from previous batches have not yet been verified or resolved-indirectly classified
- [ ] Check whether any prior "provisional milestone" needs N≥5 confirmation
- [ ] Confirm that observation infrastructure is operational: `REYN_LLM_TRACE_DUMP` captures successfully, `dogfood_trace.py` reads without errors
- [ ] Draft scenario plan (A1): include concrete user messages, exercised code paths, and four-category probability distributions for each expected outcome
- [ ] Confirm that "blocked" is included as an outcome category in your predictions
- [ ] Submit plan for user review (A2) before running anything

### Fix dispatch — checklist

Before dispatching each fix:

- [ ] **Reproduce-first gate:** run the scenario on current HEAD and confirm the bug reproduces. If it does not reproduce, classify as resolved-indirectly and document.
- [ ] **Documented design audit:** read the relevant specification document (phase.md, permission-model.md, or the relevant concept doc). Confirm that the proposed fix is consistent with the documented design.
- [ ] **Fix classification:** label the fix as either "bug fix (restoring documented design)" or "spec change (new behavior)." Communicate this classification to the user.
- [ ] **Fix layer:** apply the care boundary check (Principle 5). Is the fix structural? Behavioral rescue? Prompt rule? Aim for structural.
- [ ] **Hypothesis isolation:** if the fix addresses an LLM behavior issue, is it testing exactly one hypothesis? If multiple hypotheses are bundled, separate them.
- [ ] **Verify-first gate:** after the fix lands and tests pass, run an e2e dogfood scenario that exercises the fixed path. Confirm the fix is effective in the real artifact flow.

### Retrospective template

```markdown
# Batch N — Retrospective

> [One-sentence summary of the batch's main outcome]

## Expected vs actual

| Scenario | Prediction | Actual | Hit/Miss |
|----------|-----------|--------|---------|
| S1 | X% verified | [outcome] | hit/miss |

## Turning points

[List 2–3 moments where observation diverged from expectation, and what was learned]

## Principles reinforced or newly established

[List principles that were reinforced by this batch's data, and any new principles that emerged]

## Handoff to next batch

- Open findings: [list with severity]
- Calibration adjustments: [what to change in base rates or outcome categories]
- Infrastructure updates needed: [if any]
```

### Batch directory structure

```
docs/deep-dives/journal/dogfood/
└── YYYY-MM-DD-batch-N-{label}/
    ├── prelude.md          ← state at batch start + carry-over findings
    ├── scenarios.md        ← concrete scenarios with predictions
    ├── findings.md         ← summary table + narrative
    ├── findings/
    │   ├── BN-H1-<slug>.md ← per-finding detail (severity / status / diagnosis)
    │   └── ...
    └── retrospective.md    ← extracted principles + handoff
```

Finding ID format: `B{batch}-{Severity}{rank}-{slug}`. Severity prefixes: `H` (HIGH), `M` (MED), `L` (LOW), `INFO`. Example: `B13-H1-permission-revert.md`.

For cross-batch issues (tracked across multiple batches without resolution), use the giveup tracker: `docs/deep-dives/journal/dogfood/giveup-tracker.md`.

### Pacing: the first batch is a calibration batch

The first batch you run should be treated as a **practice batch**. Its primary purpose is not to find bugs — it is to calibrate:
- Your observation infrastructure (does `REYN_LLM_TRACE_DUMP` capture correctly?)
- Your scenario design (are the scenarios specific enough to produce clean findings?)
- Your prediction model (does your four-category distribution produce useful signal?)
- Your fix dispatch process (does the reproduce-first gate catch downstream symptoms?)

Expect that the first batch's Brier score will be high (≥0.6). This is normal. Brier score improves as your base rates become calibrated to the actual behavior of the system under test. By batch 3–4, with consistent application of the nine principles, Brier scores in the 0.3–0.4 range are achievable.

Do not declare a milestone from the first batch, even if a scenario completes successfully. Record it as a provisional data point and confirm with N≥5 in a subsequent batch.

---

## Appendix: batch case studies

The following retrospectives provide detailed case studies of the principles described in this guide, in the order they were established:

- **Batch 7** (`docs/deep-dives/journal/dogfood/2026-05-04-batch-7-post-infra-verify/retrospective.md`): Observation infrastructure established; care boundary articulated; speculation stack dissolved.
- **Batch 9** (`docs/deep-dives/journal/dogfood/2026-05-05-batch-9-fix-wave/retrospective.md`): Wrong-layer trap discovered; verify-first principle established.
- **Batch 10** (`docs/deep-dives/journal/dogfood/2026-05-05-batch-10-residual-fix-wave/retrospective.md`): Reproduce-first principle established; resolved-indirectly classification formalized.
- **Batch 13** (`docs/deep-dives/journal/dogfood/2026-05-06-batch-13-revert-and-real-milestone/retrospective.md`): Documented design audit established; fix classification discipline formalized; simplicity smell test articulated.
- **Batch 14** (`docs/deep-dives/journal/dogfood/2026-05-06-batch-14-stability-extension/retrospective.md`): Production-grade phase 1 completion; full discipline operational.
- **Batch 17** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-17-rag-phase-1/retrospective.md`): Structural pre-check necessity (= principle 10); ADR-0033 RAG Phase 1 first dogfood retracted "production grade landed" judgment; 6 release-blocker bugs fixed in 5-commit fix wave.
- **Batch 18** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-18-rag-fix-retest/retrospective.md`): Headline (S5) full recovery 0/5 → 3/3 + extended N=12 = 83% (= dogfood log's largest per-scenario calibration recovery, Brier 0.575 → 0.067); structural × behavioral prediction-axis separation (= principle 11) and verdict false-attribution discipline (= principle 12) established.
- **Batch 19 (revised post self-audit)** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-19-rag-attractor-fix-retest/retrospective.md`): Cognitive-bias named anti-attractor callout pattern validated at 100% compliance (= S9 Class A). Initially also claimed affordance-bias attractor (= Class B) established; user-prompted self-audit found the S6 evidence was confounded by a scenario design flaw (= prompt naturally matched `reyn_src_read`'s claimed use case). Class B was downgraded to hypothesis; pre-retrospective discipline established (= read LLM trace + tool description + scenario design premise BEFORE writing the retrospective).
- **Batch 20** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-20-rag-multi-source-retest/retrospective.md`): S6 redesigned with synthetic sources to remove `reyn_src_read` affordance conflict; main agent self-executed pre-retrospective discipline and caught a second scenario-design confound (= prompt structurally satisfied by single source) BEFORE writing the retrospective. Affordance-bias hypothesis remains pending; the four-dimension scenario design audit checklist (= principle 14) was lifted as the systemic fix that closes the batch-18-to-20 sequence of one-dimensional audits.
- **Batch 21** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-21-rag-real-e2e/retrospective.md`): real e2e dogfood (= 21 EN concept docs → 418 chunks via real `gemini-embedding-001`, natural concept queries instead of explicit-search prompts). First instance of: (a) main agent direct execution (= no sub-agent dispatch) of the full prelude / index / chat / audit / fix / retrospective pipeline, (b) the description/path propagation bug B21-S0-1 surfaced + fixed in-flight, (c) valid evidence for affordance-bias attractor (= 0/3 verified after the description fix landed). Lifted prompt class taxonomy (= principle 15) — batch 18 S5's 83% verified rate was P-explicit class; the 0% on natural P-natural queries was the gap the prior synthetic dogfood batches couldn't see.
- **Batch 22** (`docs/deep-dives/journal/dogfood/2026-05-10-batch-22-affordance-bias-fix/retrospective.md`): schema-layer fix for the affordance-bias attractor surfaced in batch 21. **First instance of pre-fix multi-agent context analysis (= principle 16)** — 5 parallel sonnet info-gathering agents (= trace deep-dive + industry research + description history audit + constraint audit + design space mapping) traced the true driver to a SP-level rule, not the tool description as initially assumed. Multi-layer reinforcement fix (= SP rule + 2 tool descriptions per practitioner 4-part template) landed in one commit; same N=3 retest flipped 0/3 → 3/3 verified, first attempt. Class B (= affordance-bias) hypothesis status upgraded from "partial validation" to **decisive validation**, and the schema-layer multi-layer reinforcement pattern is now the established Class B fix template.

For the full batch index and operational log, see `docs/deep-dives/journal/dogfood/README.md`.
