---
type: how-to
topic: config
audience: [human]
applies_to: [reyn.yaml, reyn.local.yaml]
---

# Localize output

**Goal:** Make a skill produce text in a chosen language without modifying the skill itself.

## How language flows

The OS injects an `output_language` field into every context frame. Phase instructions that produce user-facing text honor it ("write the answer in `{output_language}`"). The LLM picks up the cue automatically — no per-skill localization code is needed.

## Resolution order

1. `--output-language` CLI flag (`reyn run`, `reyn eval`, `reyn chat`)
2. `reyn.local.yaml` (personal override, gitignored)
3. `reyn.yaml` (project setting)
4. `~/.reyn/config.yaml` (user-global)
5. Built-in default (`ja`)

## Set per project

```yaml
# reyn.yaml
output_language: en
```

Every run in this project uses English unless overridden.

## Override per session

```bash
reyn run my_skill "..." --output-language fr
reyn chat --output-language en
```

The override only affects that run.

## Skill author guidance

Don't hardcode language strings in phase instructions. Reference `output_language` instead:

> Reply in `{output_language}`. Use a friendly, concise tone.

The runtime substitutes the resolved value. This keeps one skill working in every language the model supports.

## What this does NOT do

- It doesn't translate input. If the user types in Japanese, the LLM sees Japanese; whether it replies in `output_language` or echoes the input language depends on the prompt.
- It doesn't pick a model. Some languages may need a stronger model — choose with `--model`.
- It doesn't enforce strict language output. The LLM may slip into another language under pressure (low-confidence answers, code blocks). If strict enforcement matters, add a validation step.

## See also

- [Reference: reyn.yaml](../../../reference/config/reyn-yaml.md) — `output_language`
- [Reference: common-flags](../../../reference/cli/common-flags.md)
- [Reference: context-frame](../../../reference/runtime/context-frame.md)
