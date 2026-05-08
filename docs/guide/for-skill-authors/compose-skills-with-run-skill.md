---
type: how-to
topic: dsl
audience: [human]
applies_to: [phases/*.md, skill.md]
---

# Compose skills with `run_skill`

**Goal:** Invoke another skill from inside a skill — either deterministically before the LLM (preprocessor) or as an LLM-driven side effect (Control IR).

## Two flavors

| Flavor | Where it runs | Control |
|--------|---------------|---------|
| **Preprocessor** `run_skill` | Before the LLM, every phase visit | Skill author decides |
| **Control IR** `run_skill` | After the LLM, when it asks | LLM decides |

Pick the preprocessor when the dependency is structural (this phase always needs that skill's output). Pick Control IR when the LLM should decide whether to call.

## Preprocessor pattern

```yaml
---
type: phase
name: write_post
input: post_request
preprocessor:
  - run_skill:
      skill: recall_memory
      input:
        type: user_message
        data: { text: "what does the user prefer about post format?" }
      into: relevant_memories
---

Write the post. If `relevant_memories` includes a stated preference,
follow it; otherwise default to a 3-paragraph structure.
```

The sub-skill runs to completion. Its `final_output` artifact is bound to `input.relevant_memories`. The phase reads it like any other input field.

## Control IR pattern

For LLM-driven invocation, declare nothing in the preprocessor. The OS injects `run_skill` into `available_control_ops` and the LLM emits an op when it decides:

```json
{
  "kind": "run_skill",
  "skill": "recall_memory",
  "input": {"type": "user_message", "data": {"text": "..."}}
}
```

The phase instructions describe **when** the LLM should invoke the sub-skill, not the op's syntax (P8). Example: "If you're unsure what the user prefers, call `recall_memory` to check."

## Sub-skill nodes in the graph

A graph entry can also reference a skill directly:

```yaml
graph:
  prepare:        [@my_subskill]
  '@my_subskill': [aggregate]
  aggregate:      [end]
```

This is heavier than `run_skill`: the sub-skill becomes a graph node, not a one-shot side effect. Use it when the parent's flow genuinely waits on the sub-skill at a specific point.

## Skill resolution

All three patterns resolve names the same way:

1. `reyn/project/<name>/skill.md`
2. `reyn/local/<name>/skill.md`
3. `src/stdlib/skills/<name>/skill.md`

## See also

- [Reference: preprocessor](../../reference/dsl/preprocessor.md) — `run_skill` step
- [Reference: control-ir](../../reference/runtime/control-ir.md) — `run_skill` op
- [Reference: graph](../../reference/dsl/graph.md) — sub-skill nodes
- [iterate-with-fan-out.md](iterate-with-fan-out.md)
