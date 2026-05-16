---
type: skill
name: direct_llm
description: |
  Catalogue-gap fallback: hand a single-shot natural-language task straight to
  the LLM and return its answer verbatim. Use when no specialised skill exists
  and the task can be completed in one LLM call (translate, summarise, answer
  a knowledge question, format a snippet, reword a paragraph, etc.).
entry: respond
final_output: direct_llm_response
final_output_description: |
  The LLM's response to the user's prompt, returned as plain text.
finish_criteria:
  - The phase produced a `direct_llm_response.response` containing the answer
graph:
  respond: []
routing:
  intents: [task]
  priority: low
  tier: fallback
  when_to_use:
    - Single-shot conversational tasks where no specialised skill exists
    - Translate a paragraph, summarise pasted text, answer a knowledge question,
      format a snippet, reword some prose — anything completable in one LLM call
    - Default fallback for catalogue gaps when no other skill clearly fits
  when_not_to_use:
    - A more specific skill in the catalogue matches — prefer that one
    - Multi-step tasks (use skill_builder to design a proper skill)
    - Tasks needing file system / shell / external access
      (this skill has no permissions for side effects)
    - Tasks the user would benefit from packaging as a reusable skill
      (suggest skill_builder instead)
  examples:
    positive:
      - "翻訳して: Hello, world."
      - "要約してください: …長文…"
      - "What does 'idempotent' mean?"
      - "Format this as JSON: name=alice age=30"
      - "Reply in Japanese: how are you?"
      - "Reword this more formally: …"
    negative:
      - "Build a skill that does X"        # → skill_builder
      - "Read this file and …"             # needs file access
      - "Run a shell command"              # needs shell
      - "Schedule weekly reports"          # multi-step / scheduling
      - "Improve this skill"               # → skill_improver
# FP-0016 D: this skill needs no static secrets / OAuth tokens.
required_credentials: []
---

## Overview

`direct_llm` is the catalogue's fallback for **single-shot LLM tasks**. The
phase issues exactly one LLM call with the user's text as the question, and
returns the answer as `direct_llm_response.response`. There is no
preprocessor, no Control IR, no graph branching — just one phase that
finishes immediately.

This skill exists so the router can always route a "real work" prompt
somewhere, even when no specialised skill matches. Without it, generic
prompts like "translate this paragraph" have nowhere to go.

## Input

`user_message.text` — the user's prompt, exactly as typed. The `reyn run`
CLI auto-wraps a bare string into a `user_message` artifact, so:

```
reyn run direct_llm "Translate 'hello' to Japanese."
```

works directly. To pass an explicit JSON artifact, use:

```
reyn run direct_llm '{"type":"user_message","data":{"text":"…"}}'
```

A `direct_llm_request` artifact is also defined (with `user_prompt` plus
optional `context`) for callers that want a structured envelope, but the
phase reads from `user_message` — the catalogue's standard text-input
artifact.

## Output

`direct_llm_response.response` — the LLM's answer as plain text. Match the
language of the input.

## When NOT to use

Whenever a more specific skill matches, use that one. `direct_llm` should
be the **last-resort** match for the router — its `priority: low` /
`tier: fallback` routing metadata reflects this. Multi-step work, anything
needing the file system or shell, and anything the user would benefit
from packaging as a reusable skill should go elsewhere.
