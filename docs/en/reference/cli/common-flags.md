---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn run, reyn eval, reyn chat]
---

# Common flags

Flags shared across `reyn run`, `reyn eval`, and `reyn chat`. Per-command flags live in their own pages.

## Model selection

| Flag | Default | Description |
|------|---------|-------------|
| `--model MODEL` | `reyn.yaml` `model` (or `standard`) | Model class (`light` / `standard` / `strong`) or LiteLLM model string. Resolved via `reyn.yaml`'s `models` map. |

## Output language

| Flag | Default | Description |
|------|---------|-------------|
| `--output-language LANG` | `reyn.yaml` `output_language` (or `ja`) | Language code injected into the LLM context as `output_language`. Phases that produce user-facing text honor it. |

## Runtime limits

All limits are read from `reyn.yaml`'s `limits:` block by default and can be overridden per-invocation.

| Flag | Default | Description |
|------|---------|-------------|
| `--max-phase-visits N` | `limits.phase.max_visits` (or `25`) | Cap on revisits to any single phase in one run. `0` disables the cap. Prevents runaway revision loops. On exceed, the run ends with status `loop_limit_exceeded`. |
| `--phase-budget SECONDS` | `limits.phase.max_wall_seconds` (or `0`) | Per-phase wall-clock budget. Soft check at retry/turn boundaries — does not cancel mid-call. `0` disables. On exceed, the run ends with status `phase_budget_exceeded`. |
| `--llm-timeout SECONDS` | `limits.llm.timeout` (or `60`) | Per-call HTTP timeout passed to LiteLLM. |
| `--llm-max-retries N` | `limits.llm.max_retries` (or `3`) | Transient-error retries per LLM call (LiteLLM exponential backoff). |

## Permission gating (`reyn run` only)

| Flag | Default | Description |
|------|---------|-------------|
| `--allow-shell` | off | Enable the `shell` Control IR op. Required for skills that invoke sub-processes. |
| `--allow-untrusted-python` | off | Allow `mode: trusted` Python preprocessor steps (no AST sandbox). Pure-mode steps run without this. |
| `--strict` | off | Validate required fields at every nesting depth (default: top level only). |

## Diagnostics

| Flag | Available on | Description |
|------|--------------|-------------|
| `--rich` | `run`, `events` | Rich-styled console output. |
| `--events` | `run` | Print the full event log at the end of execution. |

## Resolution order

For each flag, the runtime checks (highest precedence first):

1. CLI flag
2. `reyn.yaml` (project) — values under matching keys
3. `.reyn/config.yaml` (personal overrides) — same schema as `reyn.yaml`
4. Built-in default

`reyn eval` adds one extra layer for `--model`: the eval spec's `model:` field sits between CLI and `reyn.yaml`.

## See also

- [run.md](run.md), [eval.md](eval.md), [chat.md](chat.md)
- [Reference: reyn.yaml](../config/reyn-yaml.md)
- [Reference: permissions](../config/permissions.md)
