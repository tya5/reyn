---
type: concept
topic: architecture
audience: [human, agent]
---

# LLM as a constrained decision engine

In reyn the LLM is not the orchestrator. It is a **decision-making node** the OS calls between transitions. The OS hands it a small, finite set of choices; the LLM picks one. Anything outside that set is rejected.

## What the LLM is allowed to choose

For every phase visit, the OS builds a context frame containing:

- the current phase's instructions and input artifact,
- the **candidate outputs** — for each allowed next phase (or `end`), the input schema it expects,
- the **available Control IR ops** — what side effects are unlocked for this phase.

The LLM responds with a single JSON object:

- `control` — pick one candidate (`transition` to a phase, or `finish`),
- `artifact` — data that conforms to the chosen target's input schema,
- `control_ir` — zero or more side-effect ops drawn from the available list.

That's the contract. There is no other channel.

## Why this is not "the LLM is a tool the OS calls"

It would be more accurate to flip the framing: the LLM is the **decision policy**, and the OS provides the constrained action space. The OS is not the LLM's tool — it's the rule-keeper that bounds what the LLM can do.

This bounding is what gives reyn its three guarantees:

- **Replayable.** A saved event log fully captures the workflow; a re-run on the same inputs follows the same edges (modulo the LLM's stochasticity within each phase).
- **Validatable.** Every artifact is checked against the target schema before the OS commits the transition. A malformed output triggers a re-prompt, not a crash and not silent drift.
- **Extensible.** Because the LLM only picks from the OS-injected candidate set, adding a new phase or new control op never requires retraining or prompt-engineering — the OS just exposes one more option.

## The "what if the LLM is wrong?" cases

| The LLM emits… | The OS does |
|----------------|-------------|
| A `next_phase` not in the graph | Reject; emit `validation_error`; re-prompt |
| An `artifact` whose `type` doesn't match | Reject; emit `validation_error`; re-prompt |
| Required schema fields missing | Reject; emit `validation_error`; re-prompt |
| Control IR ops the phase didn't declare | Reject; emit `permission_denied` |
| Free-form text outside the JSON contract | Normalizer attempts a recovery; if it fails, emit `normalization_error` |

After a configurable number of failed re-prompts the run aborts. The OS never silently fixes up the LLM's output.

## Why not give the LLM more freedom?

Unconstrained LLM control flow is unstable in three measurable ways:

1. **Drift over long runs.** Each free choice is a chance to wander off-task. Bounding the choice set keeps the trajectory in the workflow's design.
2. **Untestability.** "Will this prompt eventually finish?" is undecidable for a free agent and trivially decidable on a finite graph.
3. **No clean re-entry point.** When something goes wrong, you want to point to the failing phase. Free-form orchestration has no phases to point at.

So reyn pays the cost of writing skill graphs explicitly and gets predictability in return.

## See also

- [../architecture/principles.md](../architecture/principles.md) — P3, P4, P8
- [../architecture/phase-vs-skill-vs-os.md](../architecture/phase-vs-skill-vs-os.md)
- [Reference: llm-output-contract](../../reference/runtime/llm-output-contract.md)
- [Reference: context-frame](../../reference/runtime/context-frame.md)
