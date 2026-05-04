# Batch 5 Retest 2 — Prelude

**Date**: 2026-05-04  
**HEAD**: `ca116f3` (B5-H1 prompt re-balance) + `fe91321` (B5-H2 eval run_target instruction)  
**Model**: gemini-2.5-flash-lite (LiteLLM proxy at localhost:4000)  
**Scenarios**: A (curry / specialist) + B (skill_improver eval cascade)

## Key fixes under test

- **B5-H1** (`ca116f3`): Restore individual bullets in router prompt (1 bullet = 1 MUST),
  remove "engage the skill ecosystem" jargon. Aims to fix specialist stopping after
  `list_skills` without calling `invoke_skill`.

- **B5-H2** (`fe91321`): Clarify `run_target` phase instructions — explicit `skill` key
  in `run_skill` Control IR (not `name`/`path`), with positive + negative examples.
  Aims to fix `KeyError: 'name'` in eval cascade.

## Run environment note

Piped stdin to `reyn chat` exits as soon as the `_input_loop` reads EOF or `/quit`.
With async peer agent routing, a `/quit` sent immediately after the user message causes
`registry.shutdown()` before the specialist's router loop completes. A `sleep` delay
before `/quit` is required. All measurements in this batch use the delayed pattern.
