---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [direct_llm]
---

# `direct_llm`

Catalogue-gap fallback: hand a single-shot natural-language task straight to the LLM and return its answer verbatim.

## Entry

`respond`

## Final output

`direct_llm_response` — the LLM's answer as plain text in `response`.

## How it composes

Single-phase skill with no graph branching, no Control IR, and no preprocessor. The `respond` phase issues one LLM call (`max_act_turns: 1`) using the `user_message` artifact as input, then finishes immediately. Routing metadata sets `priority: low` / `tier: fallback` so the router always prefers a more specific skill when one matches.

## Caveats

- No filesystem, shell, or web access — the phase has `allowed_ops: []`.
- Not appropriate for multi-step tasks or anything requiring side effects.
- When a more specific skill matches, use that instead.

## Usage

Use when no specialised skill fits and the task can be completed in one LLM call (translate, summarise, answer a knowledge question, format a snippet, reword prose, etc.).

```bash
reyn run direct_llm "Translate 'hello' to Japanese."
reyn run direct_llm '{"type":"user_message","data":{"text":"What does idempotent mean?"}}'
```

## Source

[`src/reyn/stdlib/skills/direct_llm/skill.md`](https://github.com/tya5/reyn/blob/main/src/reyn/stdlib/skills/direct_llm/skill.md)
