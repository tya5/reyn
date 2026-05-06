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

---

## 6. Reyn-specific tooling

> **This section is Reyn-specific.** The principles in this section (observation infrastructure, payload inspection, replay) apply to all LLM-driven systems. The specific tools described here are Reyn's implementation of those principles. If you are adapting this discipline to a different system, see the "Adapting to other systems" paragraph at the end of this section.

### Why these tools exist

Before batch 7, LLM behavior analysis at Reyn was conducted without any mechanism to observe what the LLM actually received. Hypotheses about the LLM's behavior were formed by reading code. This produced a five-deep speculation stack that took multiple batches to unwind and cost several wrong-layer fixes.

The batch 7 observation infrastructure investment changed the iteration speed from "days per hypothesis" to "minutes per hypothesis." The four-tool kit covers: full payload capture, payload inspection, payload replay, and attractor auto-detection.

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
python scripts/detect_attractor.py --root .reyn/
```

Run this after every dogfood batch to catch attractor patterns that might not be visible in the high-level scenario outcome. A scenario can "complete" (produce a final output) while containing one or more attractor events at intermediate phases.

### Adapting to other LLM-driven systems

The core requirement is payload observability: you must be able to see what the LLM receives and produces for each call. Every LLM API provider supports capturing request/response pairs; the question is whether your system routes all calls through a capture layer.

The minimum viable observation stack:
1. A capture mechanism that writes `{call_id, system_prompt, messages, tools, response}` to a structured log for every LLM call
2. An inspection utility that can filter and display that log by call ID and field
3. A replay mechanism that can re-run a captured payload with modifications

Reyn's three tools (`REYN_LLM_TRACE_DUMP`, `dogfood_trace.py`, `llm_replay.py`) are one implementation. Any LLM proxy layer (LiteLLM proxy, custom middleware) can implement the same three capabilities. The attractor detector is a post-processing step that can be rebuilt for any domain given the captured payloads.

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
docs/journal/dogfood/
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

For cross-batch issues (tracked across multiple batches without resolution), use the giveup tracker: `docs/journal/dogfood/giveup-tracker.md`.

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

- **Batch 7** (`docs/journal/dogfood/2026-05-04-batch-7-post-infra-verify/retrospective.md`): Observation infrastructure established; care boundary articulated; speculation stack dissolved.
- **Batch 9** (`docs/journal/dogfood/2026-05-05-batch-9-fix-wave/retrospective.md`): Wrong-layer trap discovered; verify-first principle established.
- **Batch 10** (`docs/journal/dogfood/2026-05-05-batch-10-residual-fix-wave/retrospective.md`): Reproduce-first principle established; resolved-indirectly classification formalized.
- **Batch 13** (`docs/journal/dogfood/2026-05-06-batch-13-revert-and-real-milestone/retrospective.md`): Documented design audit established; fix classification discipline formalized; simplicity smell test articulated.
- **Batch 14** (`docs/journal/dogfood/2026-05-06-batch-14-stability-extension/retrospective.md`): Production-grade phase 1 completion; full discipline operational.

For the full batch index and operational log, see `docs/journal/dogfood/README.md`.
