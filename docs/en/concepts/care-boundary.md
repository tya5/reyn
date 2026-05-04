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

## See also

- [principles.md](principles.md) — P1–P8 (especially P3, P4, P7)
- [phase-vs-skill-vs-os.md](phase-vs-skill-vs-os.md) — layer boundary architecture
- [llm-as-decision-engine.md](llm-as-decision-engine.md) — why the LLM is constrained, not rescued
- [events.md](events.md) — observability as the post-call tool (P6)
