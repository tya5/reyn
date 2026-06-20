---
type: how-to
topic: runtime
audience: [human]
applies_to: [phases/*.md]
---

# Ask the user a question from inside a phase

**Goal:** Pause a phase, ask the user something, and resume the same phase with the answer merged into the input.

## When to use

- The phase has all-but-one piece of information and the missing piece can't be inferred.
- The cost of guessing wrong is higher than the cost of one extra prompt.

## Pattern

In phase instructions, describe **when** to ask. The LLM emits an `ask_user` Control IR op:

```
If `relevant_memories` does not specify the user's preferred output
language, ask the user before continuing.
```

The LLM's emitted op:

```json
{
  "kind": "ask_user",
  "question": "Which language should I write the post in?",
  "suggestions": ["English", "Japanese"]
}
```

## What the OS does

1. Prints the question (and suggestions, if given).
2. Reads stdin until a response.
3. Merges the original input + Q&A into a `user_message` artifact.
4. Re-runs the **same phase** with the merged artifact. Visit count does not increment.

Two events are emitted: `user_intervention_requested` and `user_intervention_received`.

## Phase instructions: do say / don't say

**Do say:**

- WHEN to ask (the trigger condition).
- WHAT to ask (a domain-specific question).

**Don't say:**

- The op's JSON shape (`{kind: ask_user, question: ...}`). The OS injects this into `available_control_ops` (P8).

## Caveats

- `reyn eval` is non-interactive. A skill that asks the user mid-phase will hang in eval mode. Either gate the question on a condition that's never true under eval, or pre-supply the missing field in the eval spec.
- Don't ask multiple questions in one op. Use one `ask_user` op at a time; if you need more, ask one, get the answer (which re-enters the same phase), then decide whether to ask another.

## See also

- [Reference: control-ir](../../../reference/runtime/control-ir.md) — `ask_user`
- [Reference: events](../../../reference/runtime/events.md) — `user_intervention_*` events
- [Concepts: principles P8](../../../concepts/architecture/principles.md)
