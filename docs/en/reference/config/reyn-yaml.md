---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# `reyn.yaml`

Project-level configuration. Checked in to git. Personal overrides go in `reyn.local.yaml` (gitignored) or `~/.reyn/config.yaml`.

## Minimal example

```yaml
model: standard
models:
  light:    openai/gemini-2.5-flash-lite
  standard: openai/gpt-4o
  strong:   anthropic/claude-3-5-sonnet-20241022
```

## Top-level keys

| Key | Type | Description |
|-----|------|-------------|
| `model` | string | Default model class. Resolved via `models`. Override with `--model`. |
| `models` | map | Class name → LiteLLM model string. |
| `output_language` | string | Default output language code (e.g. `en`, `ja`). Override with `--output-language`. |
| `max_phase_visits` | int | Cap on phase revisits per run. `0` = unlimited. Default `25`. |
| `state_dir` | path | Where reyn writes events, approvals, memory. Default `.reyn/`. |
| `permissions` | map | Default permission policy. See below. |

## `permissions` block

Project-wide capability defaults. Per-skill permissions in `skill.md` override these.

```yaml
permissions:
  shell: deny           # deny | ask | allow
  file:
    read:  [".reyn/", "src/stdlib/"]
    write: [".reyn/state/", "reyn/local/"]
  python:
    pure:    allow      # default for pure-mode python steps
    trusted: deny       # trusted mode also requires --allow-untrusted-python
    allowed_modules:
      - math
      - statistics
      - json
      - re
```

The full permission grammar is documented in `reference/config/permissions.md` (Phase 2).

## API keys

API keys MUST come from environment variables — `OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `GEMINI_API_KEY`, etc. Never put them in `reyn.yaml` or `reyn.local.yaml`.

## Proxy / `api_base`

If you route models through a local LiteLLM proxy, put the URL in `reyn.local.yaml` (gitignored), not `reyn.yaml`:

```yaml
# reyn.local.yaml
api_base: http://localhost:4000
```

## Resolution order

For each setting, reyn merges (lowest priority first):

1. `~/.reyn/config.yaml` (user-global)
2. `reyn.yaml` (project)
3. `reyn.local.yaml` (project, gitignored)
4. CLI flags

## See also

- `reference/config/permissions.md` — full permission grammar (Phase 2)
- `reference/config/state-dir.md` — `.reyn/` layout (Phase 2)
