---
type: concept
topic: architecture
audience: [human, agent]
---

# Care boundary — what Reyn cares about, what it doesn't

## TL;DR

Reyn prepares the structural environment in which the LLM makes decisions; it does not rescue or patch the LLM's probabilistic outputs.

## The principle

Every design decision in Reyn — whether to add a schema constraint, emit an event, or expose a new Control IR op — maps to one of three categories:

### 1. Reyn cares (structural environment, pre-call)

These are the things the OS builds *before* each LLM call so that the LLM has a sound environment to reason in:

- **Schema and enum constraints.** If a field can take only a finite set of values, those values are expressed as an enum in the artifact schema. The LLM can't hallucinate outside the set; the OS rejects anything that does. (Historical example: RETRO-H1 fixed `invoke_skill.name` by adding an enum of live skill names. The attractor vanished immediately because the constraint was structural, not instructional.)
- **Context provision.** The OS assembles a flat, current list of available skills and injects it into the system prompt. The LLM doesn't have to guess what exists.
- **Deterministic work delegation.** Anything that can be derived mechanically from the inputs — path computation, file glob, schema validation, format conversion — is done by the phase preprocessor, not the LLM. (Historical example: G2's `copy_to_work` phase was originally LLM-driven. The LLM repeatedly skipped write steps. Moving the logic to an 8-step preprocessor made the problem structurally impossible; `max_act_turns` was set to 0.)
- **Input shape normalization.** Union artifact types are resolved, OS-computed paths are injected, and the context frame is assembled — all before the LLM sees it.

These are not optional niceties. Without them, Reyn doesn't work.

### 2. Reyn does not care (behavioral rescue, post-call)

These are the things the OS deliberately does *not* do after receiving the LLM's output:

- **Retry on behavioral failure.** If the LLM produces a valid JSON output that nevertheless reflects a poor decision (wrong phase pick, underconfident choice, missing reasoning), the OS does not silently re-invoke. It emits a clean failure event and surfaces it to the user. (Historical example: Option B for G12 — auto-retry on empty `stop` — was proposed and rejected. Option F was adopted instead: emit the event, let the user decide.)
- **Fallback escalation.** The OS does not automatically switch to a stronger model when the primary model struggles. Model selection is a configuration concern, not a runtime rescue mechanism.
- **Attractor state machines.** The OS does not detect "the LLM seems stuck in a loop" and intervene with a corrective state machine that decides what the LLM should do next. That would replace the LLM as a decision node with an OS-level heuristic — a P3 violation.

The reasoning is not that LLM failures are unimportant. It is that:

1. Auto-rescue of probabilistic outputs is itself probabilistic. The OS cannot know whether a failure was transient or structural.
2. Rescuing inside the OS hides the failure from the user and from the event log — making the system harder to debug, not easier.
3. Behavioral rescue logic is a bloat trap: each new failure mode demands a new rescue arm, and the OS grows without bound.

The right tool for post-call failures is observability: emit structured events, surface them cleanly, and let the user act.

### 3. Gray zone (prompt rules — handle with care)

Prompt rules occupy a structurally ambiguous position. They are pre-call (the OS injects them into the system prompt before invoking the LLM), but they are behavioral in nature (they ask the LLM to honor a constraint voluntarily rather than enforcing it structurally).

Gray zone risks:

- **Accumulation.** Each scenario-specific fix adds a rule. Rules accumulate. After enough batch iterations, the system prompt Behaviour section bloats and rules begin to contradict. (Historical example: B2-H1 and B3-H1 both added MUST rules targeting the same `list → describe → invoke` chain — three rules encoding one intent.)
- **Over-consolidation regression.** Consolidating four rules into two paragraphs weakened the signal for weak LLMs. (Historical example: B5-H1 regression after `e90c0f2` consolidated four bullets into two paragraphs.)
- **Ignored by weak models.** A weak LLM (e.g. gemini-2.5-flash-lite) treats multi-sentence paragraphs as lower-priority than individual MUST bullets. Structural constraints enforced by schema are never ignored.

The optimal balance for prompt rules, when they are genuinely needed: **individual bullet × one MUST per bullet × wording deduplication** — split bullets, deduplicate wording, but don't merge bullets into paragraphs. And always ask first: is this a structural constraint that belongs in a schema, or is it genuinely a behavioral guideline that only a prompt can express?

## Examples

| Decision | Category | Notes |
|----------|----------|-------|
| `invoke_skill.name` enum in artifact schema (RETRO-H1) | Structural care | Hallucinated skill names became impossible |
| Preprocessor handles `copy_to_work` path resolution (G2) | Structural care | LLM write-skip attractor became structurally impossible |
| OS builds flat skill list and injects into context | Structural care | LLM has current, accurate information; no guessing |
| OS assembles union artifact before LLM call | Structural care | Input shape is always well-formed |
| Option F: emit `empty_stop` event, clean failure UX | Post-call observe-only | User sees the failure; no silent retry |
| Option B: auto-retry on `empty_stop` (rejected) | Behavioral rescue | Rejected — hides failure, P3-adjacent, OS bloat |
| Attractor OS state machine (proposed, withdrawn) | Behavioral rescue | Withdrawn — OS can't reliably know when LLM is "stuck" |
| MUST rule accumulation across batch iterations | Gray zone | Watch for bloat and cross-scenario interference |

## Why this framing

### P3: OS controls execution, not outcomes

P3 says the OS is the runtime engine. LLM is the decision policy. The moment the OS starts second-guessing the LLM's output and silently correcting it, the OS has become the decision policy — P3 is violated. The OS is permitted to reject invalid outputs (validation is structural); it is not permitted to silently substitute better ones.

### Predictability over autonomy (reyn's vision for constrained environments)

Reyn is designed for high-constraint environments where predictability matters more than autonomy. A system that silently rescues LLM failures looks more capable in demos but becomes harder to trust in production: when does the OS take over? Under what conditions? What does the event log show? Explicit failure is observable. Silent rescue is not.

### OS bloat prevention

Behavioral rescue logic compounds. Each new failure mode demands a new rescue arm. An OS that handles empty stops, then stuck attractors, then model degradation, then schema drift — each arm conditionally — becomes a second decision engine layered on top of the first. The OS grows without bound, and each new arm introduces new failure modes. Keeping the boundary clean keeps the OS linear.

## Anti-patterns

### Auto-retry on LLM behavioral failure

```
# Anti-pattern: OS decides the LLM was "wrong" and retries silently
if result.is_empty_stop():
    result = await llm.call(context, hints=["try harder"])
    # The user never sees the first failure
```

The OS should instead emit a structured event (e.g. `empty_stop`) and return a clean failure to the caller. The user decides whether to retry, escalate, or investigate.

### Attractor detection + corrective state machine

```
# Anti-pattern: OS counts consecutive same-phase visits and intervenes
if transition_count[phase] > THRESHOLD:
    next_phase = os_heuristic_pick_recovery_phase(context)
    # OS is now the decision node, not the LLM
```

If a phase is an attractor, the fix is structural: revise the skill graph to close the loop (e.g. add a `max_iterations` guard in the phase preprocessor), not add a runtime escape hatch in the OS.

### Prompt rule accumulation

```
# Anti-pattern: each failing scenario adds a MUST rule
MUST call invoke_skill after list_skills.
MUST call invoke_skill or explain after describe_skill.
After list_skills reveals a matching skill, MUST call describe or invoke.
After describe_skill, MUST call invoke_skill if the user asked for Action.
```

Each rule targets a specific scenario. They overlap, contradict, and confuse weak models. The fix is to find what structural constraint each rule is a symptom of — and enforce that constraint in the schema or graph, not the prompt.

## Related

| Memory file | Relationship to care boundary |
|-------------|-------------------------------|
| `feedback_deterministic_split.md` | One form of structural care: delegate deterministic work to preprocessors, not the LLM |
| `feedback_prompt_design.md` | The canonical gray zone trap: prompt rule bloat and over-consolidation |
| `feedback_minimize_speculation.md` | How to design care decisions: one hypothesis, one fix, one observation |
| `feedback_observe_before_speculate_llm.md` | The observability infrastructure that enables post-call observe-only: events surfaced, not patched |

The care boundary is the meta-principle that unifies these four.

## Downstream tooling — what builds on Reyn

The three care regions above describe where the OS boundary sits relative to LLM behavior. There is a fourth boundary worth naming: where Reyn ends and the ecosystem that builds *on top of it* begins.

### The pattern

Reyn exposes a set of raw primitives at the OS layer:

- **Events log** — a JSONL stream of every state change, structured and machine-readable (see [events.md](events.md)).
- **WAL and skill snapshots** — the workspace state that survives a crash; the artifact of P5's workspace-as-source-of-truth.
- **Cost tracker** — per-run and per-skill token and cost aggregations emitted as events.
- **Phase trace** — the sequence of phases, LLM calls, and Control IR executions recorded per run.
- **control_ir results** — op-level outcomes written into the event log per phase execution.

These primitives are sufficient for a range of downstream products that the LLM-agent ecosystem is actively building: conversation-analytics platforms, durable agent runtimes, eval-as-a-service, observability dashboards, agent marketplaces. Reyn provides the substrate; those products are the consumer layer.

### Why this is intentional, not accidental

P7 says OS code must not contain skill-specific strings. The same logic extends one level up: the OS must not absorb every adjacent product need. Each absorbed feature would require the OS to know something skill-specific or consumer-specific in order to provide it, defeating the abstraction that makes Reyn extensible.

The care boundary is therefore not just about the LLM-behavior split described above — it also defines the downward limit of what upstream products are expected to build themselves. Keeping Reyn small enough to be a foundation is what preserves the foundation's usefulness. An OS that tries to be the analytics platform, the deployment runtime, and the eval service simultaneously would need skill-specific knowledge at every turn — a cascade of P7 violations.

### Concrete examples from the landscape

Two products from the 2025-2026 HN AI-agent landscape illustrate the pattern:

**Conversation-analytics platforms (Lenzy AI as one example)**

Lenzy AI offers "product analytics for AI agents" — analyzing user-agent conversations to extract product insights. The Reyn primitive it would consume is the events log: `workflow_started`, `phase_completed`, `llm_called`, and per-skill aggregations already carry everything needed to reconstruct a conversation arc and derive analytics.

What Reyn does: emit structured, per-run events with stable envelopes. What is deliberately out of scope: aggregating those events across users, runs, or skills into dashboards, trend lines, or product-insight reports. That layer requires product-specific schema knowledge (what does "a successful conversation" mean for *this* skill?) that the OS must not encode.

**Stateful agent runtimes (Agentainer as one example)**

Agentainer ("Vercel for stateful AI agents") offers durable agent containers with persistent state, auto-recovery, and proxy routing. The Reyn primitives it would consume are WAL + skill snapshots + the state-dir contract — the same machinery that enables P5 crash recovery.

What Reyn does: maintain a workspace that survives a crash; resume a run from the last consistent WAL checkpoint. What is deliberately out of scope: zero-DevOps container management, HTTP proxy routing, multi-tenant state isolation, and retry policies tuned to infrastructure failure modes. Those concerns belong to the deployment layer, not the agent OS.

**Eval-as-a-service products**

What Reyn does: provide `LLMReplay` and the eval framework for per-phase, per-skill test execution. What is deliberately out of scope: hosted eval pipelines, cross-organization benchmark aggregation, or rubric marketplaces. An eval service consuming Reyn would drive `LLMReplay` via API, not require Reyn to ship the hosting infrastructure.

**Observability dashboards**

What Reyn does: emit events as structured JSONL with a stable envelope (`ts`, `kind`, `phase`, `run_id`, payload). What is deliberately out of scope: storing those events in a queryable database, rendering time-series dashboards, or alerting on anomalies. Any JSONL-compatible observability tool can ingest the log without Reyn shipping an embedded dashboard.

### The contract this implies

Because downstream consumers depend on the events log, WAL, and state-dir formats, those formats should evolve with the same care as a public API. A breaking change to the event envelope — renaming `kind`, changing `run_id` format, restructuring payload fields — is a breaking change for every analytics or observability integration built on top.

The pre-1.0 stability caveat applies: these contracts are not yet frozen. But the direction is toward stability and explicitness, not churn. Additions are safe; removals and renames require a deprecation window.

### A soft boundary line for contributors

When evaluating a proposed feature, ask: "Does providing this require Reyn to know skill-specific or consumer-specific things?"

If yes — if the feature would require the OS to understand what a "successful conversation" means, or which events to aggregate for which consumer, or what retry policy fits which deployment environment — it belongs in a downstream layer, not in the OS. That is not a rejection of the need; it is a correct assignment of responsibility. The OS provides the primitive; the downstream layer provides the product.

If no — if the feature is a general-purpose structural capability the OS can provide without knowing anything about any specific skill or consumer — it is a candidate for the OS layer.

This question is P7 applied to the product boundary, not just the code boundary.

## See also

- [principles.md](principles.md) — P1–P8 (especially P3, P4, P7)
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — layer boundary architecture
- [llm-as-decision-engine.md](llm-as-decision-engine.md) — why the LLM is constrained, not rescued
- [events.md](events.md) — observability as the post-call tool (P6)
- [architecture.md](architecture.md) — component layers and the OS-as-constant model
