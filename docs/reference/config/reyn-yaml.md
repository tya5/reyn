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
| `safety` | map | Runtime stop conditions: loop-detection caps, timeouts, on-limit policy. See below. |
| `cost` | map | Budget caps and rate limits (per-agent, daily, monthly). See below. |
| `plan` | map | Plan-mode step budget and retry tuning. See below. |
| `web` | map | SSL settings for `web_fetch` and MCP registry calls. See below. |
| `eval` | map | Trace exporter backends for `reyn eval`. See below. |
| `sandbox` | map | Sandboxed-exec backend selection and unsupported-platform policy. See below. |
| `action_retrieval` | map | FP-0034 universal catalog visibility + retrieval settings. See below. |
| `embedding` | map | RAG embedding model classes and batch settings (ADR-0033). See below. |
| `chat` | map | Chat-session compaction (head/body/tail) settings. See below. |
| `voice` | map | Voice input (Whisper) settings for the chat TUI. See below. |
| `events` | map | Audit-log rotation policy for chat-session event files. See below. |
| `skill_search` | map | BM25 skill pre-filter settings (FP-0024 Component A). See below. |
| `skill_resume` | map | Resume policy for ambiguous steps on restart. See below. |
| `self_improvement` | map | `skill_improver` apply-gate and version cap (FP-0006). See below. |
| `mcp` | map | MCP server definitions and `search_threshold`. See below. |
| `python` | map | Python preprocessor additional allowed-modules. See below. |
| `agent` | map | Agent identity for P6 event audit trail and outgoing HTTP header. See below. |
| `auth` | map | OAuth provider configurations for `reyn auth login`. See below. |
| `cron` | map | Scheduled skill executions (FP-0009 Component B). See below. |
| `permissions` | map | Default permission policy. See below. |
| `state_dir` | path | Where reyn writes events, approvals, memory. Default `.reyn/`. |
| `prompt_cache_enabled` | bool | Attach Anthropic prompt-cache markers to system prompts. Default `true`. |
| `project_context_path` | string | Markdown file injected into every phase system prompt. Default `REYN.md`. |
| `api_base` | string | LiteLLM proxy base URL. Typically set in `reyn.local.yaml` (gitignored). |

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

## `safety` block

Unified stop-condition namespace. Each value can be overridden per-invocation by the matching CLI flag. (The old `limits:` key was removed in FP-0004/0005; `safety:` is the single source of truth.)

```yaml
safety:
  loop:
    max_phase_visits: 25       # cap per phase per run; 0 = unlimited (--max-phase-visits)
    max_act_turns_per_phase: 10  # LLM ↔ op volleys per phase visit; 0 = unlimited
    max_router_calls_per_turn: 3 # chat-router calls per user turn
    max_agent_hops: 3          # maximum delegation depth
  timeout:
    llm_call_seconds: 60       # per-call HTTP timeout (--llm-timeout)
    llm_max_retries: 3         # transient-error retries per call (--llm-max-retries)
    phase_seconds: 0           # per-phase wall-clock budget; 0 = unlimited (--phase-budget)
    chain_seconds: 60          # wait for delegate reply before upstream error
  on_limit:
    mode: interactive          # interactive | unattended | auto_extend
    auto_extend_times: 1       # (auto_extend mode) number of auto-extensions
    ask_timeout_seconds: 0     # (interactive mode) user-prompt timeout; 0 = wait forever
```

### `safety.loop` fields

| Path | Type | Default | CLI flag | Description |
|------|------|---------|----------|-------------|
| `safety.loop.max_phase_visits` | int | `25` | `--max-phase-visits` | Cap on revisits to any single phase per run. `0` = unlimited. |
| `safety.loop.max_act_turns_per_phase` | int | `10` | — | LLM ↔ op volleys allowed inside one phase visit. `0` = unlimited. |
| `safety.loop.max_router_calls_per_turn` | int | `3` | — | Chat-router invocations per user turn. `0` = unlimited. |
| `safety.loop.max_agent_hops` | int | `3` | — | Maximum delegation depth (user → A → B → C = 3 hops). |
| `safety.loop.skill_calls_per_chain` | map | `{}` (unlimited) | — | Per-(chain, skill) spawn cap. `hard_limit` + `warn_ratio` sub-fields. Hybrid: loop-detection semantics, budget-style user approval on hit. |
| `safety.loop.skill_tokens_per_chain` | map | `{}` (unlimited) | — | Per-(chain, skill) token cap. `hard_limit` + `warn_ratio` sub-fields. |

### `safety.timeout` fields

| Path | Type | Default | CLI flag | Description |
|------|------|---------|----------|-------------|
| `safety.timeout.llm_call_seconds` | float (s) | `60` | `--llm-timeout` | Per-call HTTP timeout passed to LiteLLM. |
| `safety.timeout.llm_max_retries` | int | `3` | `--llm-max-retries` | Transient-error retries per LLM call (LiteLLM exponential backoff). |
| `safety.timeout.phase_seconds` | float (s) | `0` | `--phase-budget` | Per-phase wall-clock budget. Soft check at retry/turn boundaries — does not cancel mid-call. `0` = unlimited. |
| `safety.timeout.chain_seconds` | float (s) | `60` | — | How long a multi-agent chain waits for a delegate reply before synthesising an error. `0` = disabled. |

### `safety.on_limit` fields

| Path | Type | Default | Description |
|------|------|---------|-------------|
| `safety.on_limit.mode` | string | `interactive` | What happens when a loop/timeout cap fires. `interactive` (default) — prompt the user via `ask_user` for permission to extend; headless paths short-circuit cleanly to abort. `unattended` — abort immediately on hit (opt-in for CI / cron / scripted runs that cannot pause). `auto_extend` — auto-extend `auto_extend_times` times then abort. |
| `safety.on_limit.auto_extend_times` | int | `1` | Number of auto-extensions before falling through to abort. Used only when `mode: auto_extend`. |
| `safety.on_limit.ask_timeout_seconds` | float (s) | `0` | How long `interactive` mode waits for a user response. `0` (default) = wait forever; positive = abort with partial data after the window elapses. |

## `plan` block

Controls plan step execution budget and retry behavior.

```yaml
plan:
  step_max_iterations: 5   # max RouterLoop turns per step (default: 5)
  retry_limit: 3           # max auto-retries per step on failure (default: 3)
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `step_max_iterations` | integer | `5` | Maximum RouterLoop iterations one plan step may consume before being recorded as failed. |
| `retry_limit` | integer | `3` | Maximum automatic retries per step on transient errors. When exhausted, the user is prompted to extend the budget. Acts as a cost protection ceiling analogous to token limits. |

## `web` block

SSL settings for `web_fetch` and the MCP package registry (FP-0022).

```yaml
web:
  fetch:
    verify_ssl: true     # true | false | omit (default: env-var chain)
    ca_bundle: /path/to/ca-bundle.pem   # optional custom CA bundle
```

Priority chain (highest first):

| Priority | Condition | Effective SSL config |
|----------|-----------|----------------------|
| 1 | `web.fetch.ca_bundle` set | Custom CA bundle file (`verify=<path>`) |
| 2 | `web.fetch.verify_ssl: false` | Disable SSL verification (`verify=False`) — **use only in controlled environments** |
| 3 | `web.fetch.verify_ssl: true` | Force SSL verification (`verify=True`) |
| 4 | Both unset | Fall through: `SSL_VERIFY` env var → `litellm.ssl_verify` → `SSL_CERT_FILE` → `True` |

`verify_ssl` and `ca_bundle` also apply to MCP registry HTTP calls (package install).

## `eval` block

Trace exporter backends. When configured, reyn exports P6 event traces from every skill run to the listed backends (FP-0007).

```yaml
eval:
  exporters:
    - type: file
      path: .reyn/traces/        # default when no exporters are set
    - type: langfuse
      public_key: ${LANGFUSE_PUBLIC_KEY}
      secret_key: ${LANGFUSE_SECRET_KEY}
      host: https://cloud.langfuse.com   # optional; default cloud endpoint
    - type: otlp
      endpoint: http://localhost:4317
    - type: ietf_audit
      path: .reyn/audit/         # IETF Agent Audit Trail draft format
```

| `type` | Description |
|--------|-------------|
| `file` | JSON-lines file under `path`. Default backend when `exporters` is empty. |
| `langfuse` | Sends traces to a Langfuse instance. `public_key` + `secret_key` support `${VAR}` env interpolation. |
| `otlp` | OpenTelemetry Protocol; `endpoint` is the OTLP gRPC or HTTP receiver. |
| `ietf_audit` | IETF Agent Audit Trail draft format written to `path`. |

All exporters are fire-and-forget: export failures are logged but do not abort the skill run.

## `sandbox` block

Backend selection and unsupported-platform policy for `sandboxed_exec` ops (FP-0017).

```yaml
sandbox:
  backend: auto          # auto | seatbelt | landlock | noop
  on_unsupported: warn   # warn | error | ignore
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `auto` | Enforcement backend. `auto` lets the OS pick: macOS < 26 → `seatbelt` (sandbox-exec SBPL), Linux ≥ 5.13 with `sandbox-linux` extra → `landlock` (+ optional seccomp-BPF), otherwise → `noop` (audit-only, no enforcement). Explicit values force a specific backend. |
| `on_unsupported` | string | `warn` | Policy when the requested backend is unavailable on this platform. `warn` logs a WARNING and falls back to `noop`. `error` raises `RuntimeError` (fail-fast for production environments that require enforcement). `ignore` silently falls back. |

See [Reference: control-ir — `sandboxed_exec`](../runtime/control-ir.md#sandboxed_exec) for the op schema and backend selection details.

## `action_retrieval` block

FP-0034 universal catalog visibility + retrieval settings.  Provides the chat router with **universal catalog wrappers** (`list_actions` / `describe_action` / `invoke_action`) for uniform browse / describe / invoke across all skill / agent / MCP / file / memory / RAG categories.  Default ON since PR-3b-iv — operators who want the prior tools= shape can opt out with `universal_wrappers_enabled: false`.

```yaml
action_retrieval:
  universal_wrappers_enabled: true    # default since PR-3b-iv; set false to opt out
  embedding_class: null               # name in embedding.classes for search_actions
  hot_list_n: 10                      # Phase 2 — top-N freq+recency projection
  mode: default                       # default | minimal | performance (§D24)
```

> **Phase 6 cleanup (2026-05-16)**: the `hide_legacy_tools` flag was
> removed and the wrapper-only path is now the sole production
> behaviour (universal wrappers + hot-list aliases, no legacy per-kind
> tools in `tools=`). The flip was validated by dogfood batch 26 N=5
> (verified 32/35 = 91.4%, Brier 0.177, hallucination 0/35). Legacy
> handlers remain in the registry as backing implementations of the
> 4 wrappers (`invoke_action` dispatches via `universal_dispatch.py`).

### `action_retrieval` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `universal_wrappers_enabled` | bool | `true` | When `true` (default since PR-3b-iv), the router's `tools=` exposes only the 4 universal wrappers (`list_actions`, `search_actions`, `describe_action`, `invoke_action`) plus hot-list direct aliases.  Legacy per-kind tools (`invoke_skill`, `call_mcp_tool`, etc.) are no longer surfaced to the LLM but remain in the registry as wrapper backing handlers.  `search_actions` is gated separately by `embedding_class` (FP-0034 §D14).  Set `false` to disable the wrapper surface entirely (= no catalog routing; legacy tools become the only addressing path again — primarily for fixture-stability tests). |
| `embedding_class` | string \| null | `null` | Name of an entry in [`embedding.classes`](../../concepts/rag.md) to use for action-retrieval semantic search (FP-0034 §D13).  When `null` or empty, `search_actions` is excluded from `tools=` even when wrappers are enabled. Setting this also enables [eager embedding build](#reyn-chat---eager-embedding-build) on cold-start sessions to avoid Turn-1 hallucinations. |
| `hot_list_n` | int | `10` | Hot-list projection size for top-N `freq+recency` direct aliases (FP-0034 §D2 / §D24). Must be ≥ 0. `0` opts out entirely (= §D24 minimal mode). |
| `mode` | string | `"default"` | Operational mode label per §D24: `"minimal"` (max cache stability, no hot list) / `"default"` (balanced) / `"performance"` (large hot list).  Free-form string; callers layer semantics on top. |

### Quick-start — opt out

```yaml
# reyn.yaml — preserve pre-FP-0034 tools= shape
action_retrieval:
  universal_wrappers_enabled: false
```

After restart, the chat router's `tools=` includes the 3 wrappers at the tail (when enabled — default).  The LLM can call:

- `list_actions(category=["skill"])` → enumerate available skills as qualified names (e.g. `skill__index_docs`)
- `describe_action(action_name="skill__index_docs")` → fetch the input schema
- `invoke_action(action_name="skill__index_docs", args={...})` → execute via the existing handler

Resource categories (`mcp.server`, `rag.corpus`, `memory.entry`, …) also support `invoke_action` with the canonical default semantic (FP-0034 §D19).

Unknown action names return a structured error response with `suggestions` ranked by string similarity, so the LLM recovers in one turn (FP-0034 §D12).

### Compatibility note

Default `true` since PR-3b-iv. The test suite is structurally insulated from the flip (= LLMReplay tests use `FakeRouterHost` without the new accessor → `getattr` fallback returns False → recorded fixtures stay valid). The flip affects production runtime tools= shape only; operators can opt out with `universal_wrappers_enabled: false` to preserve the pre-FP-0034 byte-identical chat behaviour.

Subsequent FP-0034 phases (= system-prompt refactor for category-only listing per §D9, embedding-driven hot list and `search_actions` activation, redundant tool pruning) land in separate releases — each opt-in until verified via dogfood.

See [`docs/concepts/architecture.md`](../../concepts/architecture.md) for the tool registry / dispatch background.

## `agent` block

Runtime agent identity for audit trail and HTTP header propagation (FP-0016 Component E).

```yaml
agent:
  id: "reyn/acme/code-review-agent"  # default: reyn/<hostname>
```

### `agent` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `agent.id` | string | `reyn/<hostname>` | Stable identifier for this Reyn instance. Stamped onto every P6 event payload as `agent_id` and injected into outgoing MCP, A2A, and external HTTP requests as the `X-Reyn-Agent-Id` header (SOC2 / ISO27001 / METI v1.1 audit pattern). Recommended format: `reyn/<org>/<role>` (operator-defined). An empty string falls back to the default so leaving the field blank does not emit an empty `agent_id` into events or headers. |

The default `reyn/<hostname>` gives a fresh install a usable identity without operator action. Override in `reyn.yaml` when running multi-agent fleets or enterprise deployments that need a stable per-role identifier.

See [Concepts: multi-agent — Agent ID propagation](../../concepts/multi-agent.md) for cross-agent tracing and A2A header forwarding.

## `auth` block

OAuth provider configurations for `reyn auth login` (FP-0016 Component C). Each named entry under `auth.providers` defines an RFC 8628 Device Authorization Grant provider. Empty by default; the operator declares providers they want to authenticate against.

```yaml
auth:
  providers:
    github:
      client_id: "${secret:github_oauth_client_id}"
      device_authorization_url: "https://github.com/login/device/code"
      token_url: "https://github.com/login/oauth/access_token"
      scopes: [repo, user]
      # client_secret optional — omit for PKCE-only / public clients
      client_secret: "${secret:github_oauth_client_secret}"
    google:
      client_id: "...apps.googleusercontent.com"
      device_authorization_url: "https://oauth2.googleapis.com/device/code"
      token_url: "https://oauth2.googleapis.com/token"
      scopes: [openid, email]
      client_secret: "${secret:google_oauth_client_secret}"
      # audience: required by some providers (e.g. Auth0)
```

### `auth.providers.<name>` fields

| Field | Required | Description |
|-------|----------|-------------|
| `client_id` | yes | OAuth client identifier issued by the provider. |
| `device_authorization_url` | yes | Endpoint that returns `device_code`, `user_code`, and `verification_uri` (RFC 8628 §3.1). |
| `token_url` | yes | Endpoint that issues access and refresh tokens after the user completes authorisation (RFC 8628 §3.4). |
| `scopes` | yes (list) | OAuth scopes to request. Pass `[]` if the provider requires no scopes. |
| `client_secret` | no | For confidential clients. Omit for PKCE-only or public clients — RFC 6749 §2.3.1 permits this for installed apps. |
| `audience` | no | API audience identifier required by some providers (e.g. Auth0). Omit for providers that do not use it (e.g. GitHub, Google). |

`${secret:<key>}` values resolve at config-load time from `~/.reyn/secrets.env` (ADR-0030). Use `reyn secret set <key>` to store them.

See also:

- [Reference: `reyn auth`](../../reference/cli/auth.md) — `reyn auth login/list/revoke` commands
- [Concepts: secret handling](../../concepts/secret-handling.md) — OAuth lifecycle and credential scoping
- [Concepts: multi-agent](../../concepts/multi-agent.md) — agent identity propagation

## `cron:` block (FP-0009 Component B)

Schedule recurring skill executions. The scheduler runs as part of
`reyn web` (= started in the FastAPI lifespan) or as a foreground
process via `reyn cron run`.

```yaml
cron:
  jobs:
    - name: index_events_hourly
      skill: index_events
      schedule: "0 */6 * * *"   # every 6 hours
      input: {}
      enabled: true

    - name: weekly_ops_report
      skill: ops_report
      schedule: "0 9 * * MON"   # Monday 09:00
      input:
        since_days: 7
      enabled: true
```

### Fields

- **`name`** (required) — job identifier, unique within the schedule
- **`skill`** (required) — stdlib or project skill name to invoke
- **`schedule`** (required) — 5-field cron expression
  (minute / hour / day-of-month / month / day-of-week)
- **`input`** (optional, default `{}`) — input artifact passed to the skill
- **`enabled`** (optional, default `true`) — `false` keeps the entry in
  configuration but skips scheduling

### Cross-references

- `docs/reference/cli/cron.md` — `reyn cron run/list/status`
- `docs/concepts/operational-intelligence.md` — `index_events` /
  `ops_report` use-cases

## `permissions` block

Project-wide capability defaults. Per-skill permissions in `skill.md` override these.

```yaml
permissions:
  shell: deny           # deny | ask | allow
  file:
    read:  [".reyn/", "src/stdlib/"]
    write: [".reyn/state/", "reyn/local/"]
  python:
    safe:    allow      # default for safe-mode python steps
    unsafe:  deny       # unsafe mode also requires --allow-unsafe-python
    allowed_modules:
      - math
      - statistics
      - json
      - re
  # MCP server install is gated via file.write on .reyn/mcp.yaml +
  # http.get on the registry host. See "MCP install" below.
  file.write: allow
```

### MCP install (post-#571 collapse arc)

The legacy `permissions.mcp_install: ask | allow | deny` bool axis was removed in the #571 collapse arc (Phase 5, 2026-05-23). MCP install is now gated by the same list axes the rest of the OS uses:

```yaml
# reyn.yaml — install permissions express through file.write + http.get
permissions:
  file.write: allow      # blanket allow for .reyn/mcp.yaml (= install target)
  web.fetch: allow       # blanket allow for the registry fetch (= legacy alias)
```

For finer control, the skill's `skill.md` declares the canonical paths and hosts; `startup_guard` prompts the operator once per skill+host, and the runtime check is silent after that (= `file.write` model for paths outside the default zone, `http.get` per-host).

| Want | New shape |
|------|-----------|
| Block all installs project-wide | `file.write: deny` for `.reyn/mcp.yaml` paths, or `web.fetch: deny` for the registry host |
| Allow installs without prompting | `file.write: allow` and `web.fetch: allow` at the project scope |
| Allow only certain hosts | Skill declares `http.get: [{host: "..."}]` explicitly; wildcard `["*"]` defers to per-host prompts |

Enterprise pattern — point reyn at private / corporate registries with declarative config or env-var override:

```yaml
# reyn.yaml (project scope — committed to git)
mcp:
  registries:
    - https://mcp-registry.internal.acme.com   # private registry (tried first)
    - https://registry.modelcontextprotocol.io  # public fallback
permissions:
  web.fetch: allow      # blanket allow for registry fetches
  file.write: allow     # blanket approval for .reyn/mcp.yaml writes
```

Equivalent env-var override (= wins when both set):

```bash
# operator's shell rc / systemd unit / CI runner env
export REYN_MCP_REGISTRY_URLS="https://mcp-registry.internal.acme.com,https://registry.modelcontextprotocol.io"
```

Both the async op-handler client (`reyn.registry.client`) and the safe-mode skill-internal lookup (`reyn.safe.mcp.registry`) iterate the list in order:

- `lookup(server_id)` returns the first non-404 hit; all 404 → `None`.
- `search(query)` returns the first non-empty result list; all empty → `[]`.

This implements "private first, public fallback" semantics. Legacy singular `REYN_MCP_REGISTRY_URL` is honored as a one-item list for backward compat.

See [Concepts: permission model](../../concepts/permission-model.md) → "Collapse arc" for the full migration story and the canonical decomposition table.

> Legacy `permissions.mcp_install` keys in older `reyn.yaml` files are accepted with a `DeprecationWarning` and translate to the equivalent `file.write` / `http.get` gates during the migration window.

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

> **Note**: Per-chain skill spawn and token caps (`skill_calls_per_chain`, `skill_tokens_per_chain`) and the router call cap (`max_router_calls_per_turn`) were moved to `safety.loop` in FP-0004/0005. See the [`safety` block](#safety-block) above.

| Field | Scope | Persists | Reset |
|---|---|---|---|
| `per_agent_tokens` | per agent | in-memory | `/budget reset` or restart |
| `per_agent_cost_usd` | per agent | in-memory | `/budget reset` or restart |
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
| `call_timeout_seconds` | float | all (optional) | Per-call request timeout passed to the MCP SDK's `read_timeout_seconds`. Unset → SDK default applies (= no Reyn-level override; the SDK's transport-specific timeout governs). Set when a specific server is known to be slow or known to be quick + you want `fail-fast`. Independent of `timeout` (which is the HTTP transport's connect timeout for `type: http`). |

`${VAR}` in any string value is expanded from `os.environ` at startup (after `~/.reyn/secrets.env` is loaded). Missing variables expand to `""` and emit a runtime warning. Use `reyn secret set` to store values in `~/.reyn/secrets.env` — never paste tokens into `reyn.yaml` directly.

Servers are merged across config sources: `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml`. The merge is a shallow union on `mcp.servers` keys — a per-machine `reyn.local.yaml` can add or override a single server without re-stating the rest.

The MCP runtime is an optional dependency: install with `pip install -e ".[mcp]"` to pull in the official `mcp` Python SDK. Without the extra, configured servers are still parsed but any `mcp` op fails at dispatch.

### `mcp.search_threshold`

When the total number of MCP tools (across all connected servers) reaches this threshold, `build_tools()` switches from inlining all MCP tool schemas to using Anthropic's `tool_search_tool` (deferred-loading mode). Default `30`. Set `0` to disable.

```yaml
mcp:
  search_threshold: 30   # default; set 0 to always inline schemas
  servers:
    ...
```

See [Concepts: MCP](../../concepts/mcp.md) for the protocol overview and [How-to: use an MCP server](../../guide/for-skill-authors/use-an-mcp-server.md) for the end-to-end quickstart.

## `embedding` block

RAG embedding model classes and batch settings (ADR-0033). Built-in defaults cover the OpenAI path — no `reyn.yaml` changes are required for a fresh install with `OPENAI_API_KEY`.

```yaml
embedding:
  default_class: standard         # class to use when no class is specified
  batch_size: 100                 # texts per embedding API call (1–2048)
  max_concurrent_batches: 1       # parallel batch calls in flight (1–10)
  max_retries: 3                  # transient-error retries (0–10)
  retry_backoff: exponential      # exponential | linear
  tokenizer: cl100k_base          # tiktoken encoding for chunk-size estimation
  cost_warn_threshold: 10000      # ask_user gate fires above this estimated chunk count
  classes:
    light:
      model: openai/text-embedding-3-small
    standard:
      model: openai/text-embedding-3-small
    strong:
      model: openai/text-embedding-3-large
    # custom class with non-default API endpoint
    private:
      model: openai/text-embedding-3-small
      api_base: ${EMBEDDING_API_BASE}
```

### `embedding` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default_class` | string | `standard` | Class used when embedding ops don't specify one. Must be a key in `classes`. |
| `batch_size` | int | `100` | Texts per embedding API call. Valid range: 1–2048. |
| `max_concurrent_batches` | int | `1` | Parallel batch calls in flight. Valid range: 1–10. Values > 1 are accepted but log a warning until concurrent support lands. |
| `max_retries` | int | `3` | Transient-error retries per batch call. Valid range: 0–10. |
| `retry_backoff` | string | `exponential` | Backoff strategy: `exponential` or `linear`. |
| `tokenizer` | string | `cl100k_base` | tiktoken encoding used for chunk-size estimation. |
| `cost_warn_threshold` | int | `10000` | Estimated chunk count above which the `ask_user` gate fires before indexing. |

### `embedding.classes` entries

Each key under `embedding.classes` is a class name. Built-in defaults (`light`, `standard`, `strong`) are pre-loaded; user entries override them and can add new ones.

| Field | Required | Description |
|-------|----------|-------------|
| `model` | yes | LiteLLM model string (e.g. `openai/text-embedding-3-small`). |
| `api_base` | no | Override endpoint URL. Supports `${VAR}` interpolation. |
| `extra_body` | no | Provider-specific payload passed through to the API. |
| `extends` | no | Inherit from another class in the same `classes` dict and override specific fields. |

Built-in classes (active when `classes:` is empty or absent):

| Class | Model |
|-------|-------|
| `light` | `openai/text-embedding-3-small` |
| `standard` | `openai/text-embedding-3-small` |
| `strong` | `openai/text-embedding-3-large` |

## `chat` block

Chat-session compaction — head/body/tail token budgets that keep context concise without losing recent turns.

```yaml
chat:
  compaction:
    trigger_total_tokens: 30000   # compact when uncovered middle exceeds this
    head_size: 12                  # first N user/agent turns kept raw
    tail_size: 12                  # last N user/agent turns kept raw
    body_token_cap: 1500           # total token cap across all body summary sections
    min_compact_batch: 5           # skip compaction when fewer than N turns to absorb
    section_token_caps:
      topic_arc: 200
      decisions: 400
      pending: 400
      session_user_facts: 200
      artifacts_referenced: 300
```

### `chat.compaction` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `trigger_total_tokens` | int | `30000` | Compact when the uncovered middle of the conversation exceeds this token count. |
| `head_size` | int | `12` | Number of earliest user/agent turns kept verbatim (never summarised). |
| `tail_size` | int | `12` | Number of most-recent user/agent turns kept verbatim. |
| `body_token_cap` | int | `1500` | Total token budget for all body summary sections combined. |
| `min_compact_batch` | int | `5` | Skip compaction when fewer than this many turns would be absorbed (avoids tiny compactions). |

### `chat.compaction.section_token_caps` fields

| Field | Default | Description |
|-------|---------|-------------|
| `topic_arc` | `200` | Token cap for the topic-arc summary section. |
| `decisions` | `400` | Token cap for the decisions section. |
| `pending` | `400` | Token cap for the pending-items section. |
| `session_user_facts` | `200` | Token cap for user-facts carried across compactions. |
| `artifacts_referenced` | `300` | Token cap for artifact reference listings. |

## `events` block

Audit-log rotation policy for chat-session event files (PR20). Skill-run events use one file per run and are not affected by this setting.

```yaml
events:
  max_bytes: 10485760       # rotate at 10 MB (default)
  max_age_seconds: 86400    # rotate after 1 day (default)
  cleanup_period_days: null # null = no automatic deletion (default)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_bytes` | int | `10485760` (10 MB) | Rotate the active event file when it exceeds this size. `0` = no size-based rotation. |
| `max_age_seconds` | int | `86400` (1 day) | Rotate the active event file when it exceeds this age in seconds. `0` = no age-based rotation. |
| `cleanup_period_days` | int \| null | `null` | How long closed event files are kept before `reyn events purge` may delete them. `null` disables automatic deletion. `0` is rejected — use `null` to disable. |

Setting both `max_bytes` and `max_age_seconds` to `0` disables rotation entirely.

## `voice` block

Voice-input (Whisper) settings for the chat TUI (Ctrl+R to record). Optional — requires `pip install 'reyn[voice]'` (`sounddevice` + `faster-whisper`). The block is lazy-loaded; a missing `[voice]` extra silently disables the record key.

```yaml
voice:
  enabled: true           # set false to disable Ctrl+R even if deps are installed
  model: small            # tiny | base | small | medium | large-v3
  language: ja            # ISO 639-1 code; "" or null = auto-detect
  device: cpu             # cpu | cuda
  compute_type: int8      # int8 | float16 | float32
  sample_rate: 16000      # Whisper expects 16 kHz mono
  cpu_threads: 4          # 0 = OpenMP default
  num_workers: 1          # parallel transcription streams
  max_duration_s: 300.0   # auto-cancel recordings longer than this (seconds)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | Set `false` to hard-disable Ctrl+R even when deps are installed. |
| `model` | string | `small` | Whisper model size: `tiny` / `base` / `small` / `medium` / `large-v3`. |
| `language` | string \| null | `ja` | ISO 639-1 language code. `""` or `null` enables auto-detection (less reliable for short clips). |
| `device` | string | `cpu` | Inference device: `cpu` or `cuda`. `auto` is not supported — it picks the wrong device on some Mac setups. |
| `compute_type` | string | `int8` | Quantisation: `int8` / `float16` / `float32`. |
| `sample_rate` | int | `16000` | Sample rate (Hz). Whisper expects 16 kHz mono — do not change. |
| `cpu_threads` | int | `4` | CPU threads for faster-whisper. `0` = OpenMP default. Pinning to 4 avoids OpenMP/Python-threading deadlocks on Apple Silicon. |
| `num_workers` | int | `1` | Parallel transcription streams. `1` keeps memory + thread usage low. |
| `max_duration_s` | float | `300.0` | Auto-cancel recordings longer than this (seconds). Prevents runaway memory growth from unattended recordings. |

## `skill_search` block

BM25 skill pre-filter settings (FP-0024 Component A). When the catalogue exceeds `threshold` skills, the router narrows the available skill enum to the top `top_k` BM25 keyword matches before building `tools=`. Falls through to the full enum when BM25 returns zero results — no skill is ever silently hidden.

```yaml
skill_search:
  threshold: 20    # catalogue size at which BM25 activates; 0 = always filter
  top_k: 5         # number of skills returned by BM25
  backend: bm25    # bm25 (default); embedding / hybrid reserved for future phases
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `threshold` | int | `20` | Catalogue size at which BM25 pre-filtering activates. Set `0` to always pre-filter; set a high number to effectively disable. |
| `top_k` | int | `5` | Number of best-matching skills returned by BM25. Minimum `1`. |
| `backend` | string | `bm25` | Search backend. `bm25` is the only active backend; `embedding` and `hybrid` are reserved for future phases. |

## `skill_resume` block

Resume policy for skill runs interrupted mid-step. An *ambiguous step* is one whose `step_started` WAL event has no matching `step_completed` / `step_failed` — the op may have committed externally.

```yaml
skill_resume:
  default: retry            # retry | skip | discard_skill | prompt
  per_skill:
    my_idempotent_skill: retry
    my_side_effect_skill: discard_skill
```

| Policy | Description |
|--------|-------------|
| `retry` (default) | Re-execute the ambiguous step. Safe for read-only ops and skills the operator trusts to be idempotent. Risk: duplicate side effects. |
| `skip` | Synthesise an empty/default completion and continue. Risk: missing data downstream. |
| `discard_skill` | Abort the entire skill run, drop the checkpoint, and surface a failure to the originating chain. |
| `prompt` | Legacy/no-op. Retained for config compatibility; treated as `retry` by the auto-resume runtime (no interactive prompt is shown). |

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `default` | string | `retry` | Default resume policy for all skills. |
| `per_skill` | map | `{}` | Per-skill policy overrides. Key is the skill name; value is one of the policies above. |

## `self_improvement` block

`skill_improver` behavior knobs (FP-0006). Controls how the skill improver applies proposed changes back to the skill source.

```yaml
self_improvement:
  on_propose: ask_user   # ask_user | auto | disabled
  max_versions: 10       # max v<N>.md snapshots kept; 0 = no pruning
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `on_propose` | string | `ask_user` | What `skill_improver` does when about to apply improvements. `ask_user` — pause and prompt the user via the intervention `RequestBus` (safe default). `auto` — skip the prompt and apply directly (for CI / unattended runs). `disabled` — log a `skill_improvement_dry_run` event and do NOT apply changes. |
| `max_versions` | int | `10` | Maximum `v<N>.md` snapshots kept under `.reyn/skill-versions/<name>/`. Oldest version is deleted when the cap is exceeded (the current version is never deleted). `0` = disable pruning. |

## `python` block

Python preprocessor settings. Extends the built-in safe-mode allowlist of importable modules.

```yaml
python:
  allowed_modules:
    - math
    - statistics
    - json
    - re
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `allowed_modules` | list[string] | `[]` | Additional module names that safe-mode Python preprocessor steps may import, on top of the built-in stdlib allowlist. Libraries with internal I/O (e.g. `pandas`, `requests`) defeat safe-mode sandboxing — curate carefully. |

> Unsafe Python steps (`mode: unsafe` in the preprocessor frontmatter) are not restricted by this list and also require `--allow-unsafe-python` at runtime. See [Reference: permissions](permissions.md) for the full permission grammar.

## See also

- `reference/config/permissions.md` — full permission grammar
- `reference/config/state-dir.md` — `.reyn/` layout
- [Concepts: MCP](../../concepts/mcp.md)
- [Concepts: secret handling](../../concepts/secret-handling.md) — `~/.reyn/secrets.env` and `${VAR}` interpolation
- [Reference: `reyn secret`](../../reference/cli/secret.md) — managing secrets via CLI
- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — MCP server management CLI
