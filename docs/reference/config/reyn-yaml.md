---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# `reyn.yaml`

Project-level configuration. Checked in to git. Personal overrides go in `reyn.local.yaml` (gitignored, project root) or `~/.reyn/config.yaml` (user-global).

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
| `models` | map | Class name → LiteLLM model string **or** dict (see below). |
| `output_language` | string | Default output language code (e.g. `en`, `ja`). Override with `--output-language`. |
| `limits` | map | Runtime bounds: phase visits, wall-clock budgets, LLM timeouts/retries. See below. |
| `state_dir` | path | Where reyn writes events, approvals, memory. Default `.reyn/`. |
| `permissions` | map | Default permission policy. See below. |

## `models` block

Each entry under `models:` maps a class name to a LiteLLM model string **or** a dict that declares per-class LLM parameters.

### str form — literal (backward compatible)

If a str value **contains `/`**, it is treated as a literal LiteLLM model string:

```yaml
models:
  light:    openai/gemini-2.5-flash-lite
  standard: openai/gpt-4o
  strong:   anthropic/claude-3-5-sonnet-20241022
```

All existing `reyn.yaml` files using str form continue to work without change.

### str form — class reference shorthand (new)

If a str value **has no `/`**, it is a shorthand for `{extends: <name>}`.  The name
is resolved against the flat namespace (user entries + built-in catalog):

```yaml
models:
  standard: claude-sonnet-thinking     # equivalent to: standard: {extends: claude-sonnet-thinking}
```

An unknown shorthand (name not in user entries or built-ins) is a startup error.

### dict form — plain kwargs

```yaml
models:
  standard: openai/gemini-2.5-flash-lite   # str form still OK alongside dict entries

  strong:
    model: anthropic/claude-3-7-sonnet      # required
    temperature: 0.0
    max_completion_tokens: 16000             # preferred over max_tokens — see note
    extra_body:
      thinking:
        type: enabled
        budget_tokens: 8000
```

| Field | Required | Description |
|-------|----------|-------------|
| `model` | yes | LiteLLM model string. |
| `temperature` | no | Sampling temperature passed to litellm. |
| `max_completion_tokens` | no | **Preferred** max output tokens (enforced by OpenAI o1+ and most providers). |
| `max_tokens` | no | Legacy soft hint — ignored by many providers. Prefer `max_completion_tokens`. |
| `top_p` | no | Top-p sampling passed to litellm. |
| `extra_body` | no | Provider-specific payload (e.g. `thinking` for reasoning models). |
| `extends` | no | Inherit from a named class and deep-merge overrides (see below). |
| *(any other field)* | no | Silently passed through to litellm (passthrough policy). |

> **Cost limit**: use `max_completion_tokens`, not `max_tokens`.  `max_tokens` is a legacy
> soft hint that many providers ignore; it has no enforcement power on OpenAI o1+ or
> Anthropic models.  `max_completion_tokens` is enforced at the API level.

**Field policy**: `model` is the only required field. All other fields are passed directly to `litellm.acompletion` without validation — unknown fields are silently forwarded (future-proof). Typos cause silent litellm failures, not reyn errors.

**Skill / phase override**: NOT supported. Operator config (`reyn.yaml`) is the single source of truth for LLM parameters. Skill authors specify class names only (e.g. `model_class: strong`).

**Merge order**: Reyn-managed settings (`timeout`, `num_retries`, proxy routing) always take precedence over operator-declared kwargs so proxy configuration is never bypassed.

### dict form — `extends` field (new)

Use `extends` to inherit from another class and override specific fields.  The referenced
name is resolved against the same flat namespace (user entries + built-in catalog).

```yaml
models:
  # Inherit claude-sonnet-thinking built-in, reduce budget_tokens from 8000 → 4000.
  # extra_body.thinking.type: enabled is carried from the base (deep merge).
  reasoning-light:
    extends: claude-sonnet-thinking
    extra_body:
      thinking:
        budget_tokens: 4000

  # Multi-level: reasoning-heavy extends the user-defined reasoning-light above.
  reasoning-heavy:
    extends: reasoning-light
    extra_body:
      thinking:
        budget_tokens: 16000
    max_completion_tokens: 32000
```

**Deep merge**: nested dicts are merged recursively.  Only the keys you specify under
`extra_body.thinking` are overridden; sibling keys (e.g. `type: enabled`) are carried
from the base.  Scalars and lists are replaced, not merged.

**Multi-level chains**: any depth is allowed.  Reyn resolves the full chain at startup.

**Cycle detection**: circular `extends` references (e.g. `A extends B, B extends A`) are
detected at startup and raise a configuration error.

**Unknown references**: referencing a name not in the namespace (user entries or
built-in catalog) is a startup error.

### Built-in catalog

Reyn ships a built-in catalog of common model classes pre-loaded into the namespace.
You can reference them by name without declaring them in `reyn.yaml`:

| Class name | Provider / model | Notes |
|---|---|---|
| `claude-sonnet` | `anthropic/claude-3-7-sonnet` | |
| `claude-sonnet-thinking` | `anthropic/claude-3-7-sonnet` + thinking enabled | budget_tokens=8000 |
| `claude-haiku` | `anthropic/claude-3-5-haiku` | |
| `gpt-4o-mini` | `openai/gpt-4o-mini` | |
| `gpt-4o` | `openai/gpt-4o` | |
| `gemini-flash-lite` | `openai/gemini-2.5-flash-lite` | |
| `gemini-3.1-flash-preview` | `openai/gemini-3.1-flash-preview` | |
| `gemini-2.0-flash` | `openai/gemini-2.0-flash` | thinking disabled via `thinking_budget=0` |

User-declared entries **override** built-ins with the same name.  The built-in catalog
is a convenience starting point; your `reyn.yaml` is always the source of truth.

See [Reference: built-in models](../builtin-models.md) for per-entry details.

## `limits` block

Central place for runtime bounds. Each value can be overridden per-invocation by the matching CLI flag.

```yaml
limits:
  llm:
    timeout: 60        # seconds per LLM HTTP call (--llm-timeout)
    max_retries: 3     # transient-error retries per call (--llm-max-retries)
  phase:
    max_visits: 25         # cap per phase per run; 0 = unlimited (--max-phase-visits)
    max_wall_seconds: 0    # per-phase wall-clock budget; 0 = unlimited (--phase-budget)
```

| Path | Type | Default | Description |
|------|------|---------|-------------|
| `limits.llm.timeout` | float (s) | `60` | Per-call HTTP timeout passed to LiteLLM. |
| `limits.llm.max_retries` | int | `3` | Transient-error retries per LLM call (LiteLLM exponential backoff). |
| `limits.phase.max_visits` | int | `25` | Cap on revisits to any single phase per run. `0` = unlimited. |
| `limits.phase.max_wall_seconds` | float (s) | `0` | Per-phase wall-clock budget. Soft check at retry/turn boundaries — does not cancel mid-call. `0` = unlimited. |

The legacy top-level `max_phase_visits` key is still accepted (with a deprecation warning) and is migrated to `limits.phase.max_visits`.

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
  mcp_install: ask      # deny | ask | allow (default: ask)
```

### `permissions.mcp_install`

Controls whether MCP servers can be added to the configuration via `reyn mcp install` or the `mcp_install` Control IR op. Three tiers:

| Value | Behaviour |
|-------|-----------|
| `ask` (default) | Interactive prompt on first install per server. Approval is persisted to `.reyn/approvals.yaml` under key `mcp_install:<server_id>`. |
| `allow` | Install proceeds without a prompt. Useful when combined with a private registry to implement "approved servers only" policy. |
| `deny` | All install attempts are rejected. Appropriate for project-scope `reyn.yaml` in team settings where the server list is centrally managed. |

The setting participates in the standard scope-tier merge — you can set `deny` in project-scope `reyn.yaml` and allow individual developers to override with `mcp_install: ask` in `reyn.local.yaml`.

Enterprise pattern — restrict installs to a private registry:

```yaml
# reyn.yaml (project scope — committed to git)
mcp:
  registries:
    - https://mcp-registry.internal.acme.com/    # private registry first
    - https://registry.modelcontextprotocol.io/   # public fallback
permissions:
  mcp_install: allow    # team can install, but only from the registry above
```

See [Concepts: permission model](../../concepts/permission-model.md#mcp_install-permission) for the full interaction with scope tiers and the enterprise use case.

The full permission grammar is documented in `reference/config/permissions.md`.

## `${VAR}` interpolation {#var-interpolation}

Any string field in any section of `reyn.yaml` (or `reyn.local.yaml` / `~/.reyn/config.yaml`) can reference an environment variable using `${VAR}` syntax. Variables are resolved from `os.environ` at startup, after `~/.reyn/secrets.env` is loaded into the environment (see [Concepts: secret handling](../../concepts/secret-handling.md)).

```yaml
# reyn.yaml — ${VAR} works in every string field
models:
  default-sonnet:
    model: claude-sonnet-4-5
    api_key: ${ANTHROPIC_API_KEY}          # LLM API key — resolved from secrets.env or shell
    extra_body:
      headers:
        Authorization: ${LITELLM_PROXY_TOKEN}

litellm:
  api_base: ${LITELLM_API_BASE}            # LiteLLM proxy URL

mcp:
  servers:
    github:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_PERSONAL_ACCESS_TOKEN}
    internal_tools:
      type: http
      url: https://tools.example.internal/mcp
      headers:
        Authorization: "Bearer ${INTERNAL_TOOLS_TOKEN}"
```

Resolution rules:

- `${VAR}` — expands to the env var value; emits a warning and expands to `""` if undefined (never a hard error).
- `$$` — literal `$` sign (escape).
- All string fields in all YAML sections are scanned recursively, including nested dicts and lists.
- Shell environment variables take priority over `~/.reyn/secrets.env` values.

To manage `~/.reyn/secrets.env`, use `reyn secret set` / `reyn secret list` / `reyn secret clear` (see [Reference: `reyn secret`](../../reference/cli/secret.md)).

## API keys

API keys and tokens MUST come from environment variables, not from literal values in `reyn.yaml`. The recommended pattern is:

1. Store the value once: `reyn secret set ANTHROPIC_API_KEY`
2. Reference it in `reyn.yaml`: `api_key: ${ANTHROPIC_API_KEY}`

Never paste token values inline in `reyn.yaml` or `reyn.local.yaml` — they are committed to git and readable by anyone with repo access.

## Proxy / `api_base`

If you route models through a local LiteLLM proxy, put the URL in `reyn.local.yaml` (gitignored), not `reyn.yaml`. You can reference an env var here too:

```yaml
# reyn.local.yaml
api_base: ${LITELLM_API_BASE}    # or literal: http://localhost:4000
```

## Resolution order

For each setting, reyn merges (lowest priority first):

1. `~/.reyn/config.yaml` (user-global)
2. `reyn.yaml` (project, committed)
3. `reyn.local.yaml` (project, gitignored — human edits + tool writes)
4. CLI flags

**`<project>/.reyn/config.yaml` was removed in ADR-0031.** If that file still exists
on disk, Reyn emits a deprecation warning and does **not** load it. Move its contents
to `reyn.local.yaml`, then delete the file.

## `cost` block

Budget caps and rate limits. All fields are optional; omitting a field (or setting its `hard_limit` to `null`) means **unlimited**.

```yaml
cost:
  # Per-agent caps (in-memory, reset on restart or /budget reset)
  per_agent_tokens:
    hard_limit: 50000    # refuse after this many tokens for one agent
    warn_ratio: 0.8      # warn at 80% of hard_limit (default: 0.8)
  per_agent_cost_usd:
    hard_limit: 2.00     # refuse after $2.00 spent by one agent

  # Per-chain per-skill caps (in-memory)
  per_chain_skill_calls:
    hard_limit: 5        # refuse if the same skill is spawned > 5 times per chain
  per_chain_skill_tokens:
    hard_limit: 100000   # refuse if one skill accumulates > 100k tokens in a chain

  # Per-model rate limit (calls per minute)
  rate_limit_per_minute:
    openai/gpt-4o: 60
  rate_limit_warn_ratio: 0.8   # warn at 80% of rate limit

  # Daily / monthly quota (persistent across process restarts — PR25)
  # Stored in .reyn/state/budget_ledger.jsonl; reset automatically at midnight / month boundary.
  daily_tokens:
    hard_limit: 100000   # refuse after 100k tokens today
    warn_ratio: 0.8
  daily_cost_usd:
    hard_limit: 5.00     # refuse after $5.00 today
  monthly_tokens:
    hard_limit: 1000000  # refuse after 1M tokens this month
  monthly_cost_usd:
    hard_limit: 50.00    # refuse after $50.00 this month
```

| Field | Scope | Persists | Reset |
|---|---|---|---|
| `per_agent_tokens` | per agent | in-memory | `/budget reset` or restart |
| `per_agent_cost_usd` | per agent | in-memory | `/budget reset` or restart |
| `per_chain_skill_calls` | per chain+skill | in-memory | chain resolves or `/budget reset` |
| `per_chain_skill_tokens` | per chain+skill | in-memory | chain resolves or `/budget reset` |
| `rate_limit_per_minute` | per model | in-memory (60s window) | automatic (sliding window) |
| `daily_tokens` | process-global | ledger file | midnight (local time) |
| `daily_cost_usd` | process-global | ledger file | midnight (local time) |
| `monthly_tokens` | process-global | ledger file | 1st of month (local time) |
| `monthly_cost_usd` | process-global | ledger file | 1st of month (local time) |

**Cap behavior:** when a hard limit is exceeded, the LLM call is refused before it is made. Use `/budget` to see current usage and `/budget reset` to clear in-memory counters (daily/monthly are not affected by reset — they are backed by the persistent ledger).

**Ledger location:** `.reyn/state/budget_ledger.jsonl` — one record per LLM call, append-only with fsync. This file is **not** rotated automatically; it grows at roughly a few MB per month and can be manually archived if needed.

## MCP servers

External tool servers reyn can call via the [Model Context Protocol](../../concepts/mcp.md). Each entry under `mcp.servers:` is keyed by a short name (the same name the skill declares in `permissions.mcp` and emits in `mcp` ops).

The recommended way to add a server is `reyn mcp install <server_id>` (see [Reference: `reyn mcp`](../../reference/cli/mcp.md)) — it writes the entry below automatically and handles credentials via `~/.reyn/secrets.env`. Manual config is also fully supported.

```yaml
mcp:
  servers:
    # stdio: local process, JSON-RPC over stdin/stdout (most official servers)
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      env:
        FS_LOG_LEVEL: "info"

    # stdio with credential from ~/.reyn/secrets.env
    github:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_PERSONAL_ACCESS_TOKEN}

    # http: hosted server, JSON-RPC over Streamable HTTP
    internal_tools:
      type: http
      url: https://tools.example.internal/mcp
      headers:
        Authorization: "Bearer ${INTERNAL_TOOLS_TOKEN}"
```

| Field | Type | Required for | Description |
|-------|------|--------------|-------------|
| `type` | string | all | `stdio` \| `http` \| `sse` |
| `command` | string | stdio | Executable to spawn. |
| `args` | list[string] | stdio (optional) | Argument vector passed to `command`. |
| `env` | map[string,string] | stdio (optional) | Extra environment variables for the spawned process. Values support `${VAR}` expansion. |
| `url` | string | http, sse | Endpoint URL. |
| `headers` | map[string,string] | http, sse (optional) | Static request headers. Values support `${VAR}` expansion. |

`${VAR}` in any string value is expanded from `os.environ` at startup (after `~/.reyn/secrets.env` is loaded). Missing variables expand to `""` and emit a runtime warning. Use `reyn secret set` to store values in `~/.reyn/secrets.env` — never paste tokens into `reyn.yaml` directly.

Servers are merged across config sources: `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml`. The merge is a shallow union on `mcp.servers` keys — a per-machine `reyn.local.yaml` can add or override a single server without re-stating the rest.

The MCP runtime is an optional dependency: install with `pip install -e ".[mcp]"` to pull in the official `mcp` Python SDK. Without the extra, configured servers are still parsed but any `mcp` op fails at dispatch.

See [Concepts: MCP](../../concepts/mcp.md) for the protocol overview and [How-to: use an MCP server](../../guide/for-skill-authors/use-an-mcp-server.md) for the end-to-end quickstart.

## See also

- `reference/config/permissions.md` — full permission grammar
- `reference/config/state-dir.md` — `.reyn/` layout
- [Concepts: MCP](../../concepts/mcp.md)
- [Concepts: secret handling](../../concepts/secret-handling.md) — `~/.reyn/secrets.env` and `${VAR}` interpolation
- [Reference: `reyn secret`](../../reference/cli/secret.md) — managing secrets via CLI
- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — MCP server management CLI
