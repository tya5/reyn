---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn chat, reyn run-once]
---

# Common flags

Flags shared across `reyn chat` and `reyn run-once`. Per-command flags live in their own pages.

## Model selection

| Flag | Default | Description |
|------|---------|-------------|
| `--model MODEL` | `reyn.yaml` `model` (or `standard`) | Model class (`light` / `standard` / `strong`) or LiteLLM model string. Resolved via `reyn.yaml`'s `models` map. |

## Output language

| Flag | Default | Description |
|------|---------|-------------|
| `--output-language LANG` | `reyn.yaml` `output_language` (or `ja`) | Language code injected into the LLM context as `output_language`. Phases that produce user-facing text honor it. |

## Runtime limits

All limits are read from `reyn.yaml`'s `safety:` block by default and can be overridden per-invocation.

| Flag | Default | Description |
|------|---------|-------------|
| `--llm-timeout SECONDS` | `safety.timeout.llm_call_seconds` (or `60`) | Per-call HTTP timeout passed to LiteLLM. |
| `--llm-max-retries N` | `safety.timeout.llm_max_retries` (or `3`) | Transient-error retries per LLM call (LiteLLM exponential backoff). |

## Removed flags

`argparse` reports an unrecognised flag without saying why it went away, so
removals are recorded here.

| Flag | Removed | Why |
|------|---------|-----|
| `--phase-budget SECONDS` | #2696 (2026-07) | Set `safety.timeout.phase_seconds`, which **no runtime code read** — the phase engine that enforced it was deleted (#2434 / #2438). Accepting the flag and doing nothing misrepresented the run as bounded, so both the flag and the config key are gone. There is no replacement: to bound a run's wall-clock, bound the loop instead (`--max-iterations` / `safety.loop.max_router_iterations`). |

## Resolution order

For each flag, the runtime checks (highest precedence first):

1. CLI flag
2. `reyn.yaml` (project) — values under matching keys
3. `.reyn/config.yaml` (personal overrides) — same schema as `reyn.yaml`
4. Built-in default

## See also

- [chat.md](chat.md), [run-once.md](run-once.md)
- [Reference: reyn.yaml](../config/reyn-yaml.md)
- [Reference: permissions](../config/permissions.md)
