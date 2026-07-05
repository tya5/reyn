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
  light:    gemini-flash-lite
  standard: openai/gpt-4o
  strong:   anthropic/claude-3-5-sonnet-20241022
```

## Top-level keys

| Key | Type | Description |
|-----|------|-------------|
| `model` | string | Default model class. Resolved via `models`. Override with `--model`. |
| `models` | map | Class name → LiteLLM model string **or** dict (see below). |
| `model_class_by_purpose` | map | Per-purpose model-class override (`router` / `control_ir` / `tool` / `compaction` / `judge`). Unset purpose → `model`. See below. |
| `output_language` | string | Default output language code (e.g. `en`, `ja`). Override with `--output-language`. |
| `safety` | map | Runtime stop conditions: loop-detection caps, timeouts, on-limit policy. See below. |
| `cost` | map | Budget caps and rate limits (per-agent, daily, monthly). See below. |
| `web` | map | SSL settings for `web_fetch` and MCP registry calls. See below. |
| `eval` | map | Trace exporter backends for `reyn eval`. See below. |
| `sandbox` | map | Sandboxed-exec backend selection, unsupported-platform policy, and the agent-level sandbox policy. See below. |
| `hooks` | list | Agent-lifecycle hooks — template_push / shell_exec / shell_push hooks at lifecycle points. See below. |
| `action_retrieval` | map | Universal catalog visibility + retrieval settings. See below. |
| `embedding` | map | RAG embedding model classes and batch settings. See below. |
| `chat` | map | Chat-session compaction settings. See below. |
| `voice` | map | Voice input (Whisper) settings for the chat TUI. See below. |
| `events` | map | Audit-log rotation policy for chat-session event files. See below. |
| `tool_use` | map | Per-layer tool-use scheme selector (chat/step/phase). See below. |
| `mcp` | map | MCP server definitions and `search_threshold`. See below. |
| `python` | map | Python preprocessor additional allowed-modules. See below. |
| `agent` | map | Agent identity for P6 event audit trail and outgoing HTTP header. See below. |
| `auth` | map | OAuth provider configurations for `reyn auth login`. See below. |
| `cron` | map | Scheduled skill executions. See below. |
| `external_transports` | map | Inbound transport → MCP tool routing for chat (Slack / LINE / Discord etc.). See below. |
| `multimodal` | map | Binary media (image/audio) size cap, on-oversize behaviour, and artefact storage paths. See below. |
| `permissions` | map | Default permission policy. See below. |
| `prompt_cache_enabled` | bool | Attach Anthropic prompt-cache markers to system prompts. Default `true`. |
| `project_context_path` | string | Markdown file injected into every phase system prompt. Unset (default): auto-resolves the cross-tool standard — `AGENTS.md` if present, else `REYN.md` (legacy fallback). Set an explicit path to pin one file; set `""` to disable. See note below. |
| `api_base` | string | LiteLLM proxy base URL. Typically set in `reyn.local.yaml` (gitignored). |
| `tool_calls_op_loop_skills` | list | **Transitional.** Skill names opted into the native-tools op-loop — the phase act-loop drives the shared `RouterLoop.run_loop` (the converged op-loop): ops are emitted as native `tool_calls`, run through the shared executor, and threaded as native tool-role message-history. Default empty = all skills use json-mode (unchanged). Removed once the op-loop becomes the default. |

> **Project context file (`project_context_path`).** Left unset, Reyn reads
> `AGENTS.md` — the cross-tool convention that Claude Code, Codex, opencode and
> others also read — so a project shared with those tools works as-is, with no
> Reyn-specific file. If `AGENTS.md` is absent, Reyn falls back to `REYN.md`
> (legacy). The first existing file wins, and a present-but-empty `AGENTS.md` is
> authoritative (it does not fall through to `REYN.md`).
>
> **Migration.** Existing `REYN.md` projects keep working unchanged; new projects
> should prefer `AGENTS.md`. To pin a specific file regardless of the standard,
> set `project_context_path` to that path; set it to `""` to inject no project
> context at all.

## `models` block

Each entry under `models:` maps a class name to a LiteLLM model string **or** a dict that declares per-class LLM parameters.

### Model classes vs model names — the resolution rule

Two kinds of position appear in config, and they follow opposite rules. The same rule applies to the completion `models:` block **and** the `embedding.classes:` block.

- **Class position** (a *reference* to a class): `model`, per-agent / per-phase / per-op model overrides, `embedding_class`. These are **closed-world** — the value must name a class that exists in `models:` / `embedding.classes:` (or a built-in tier: `light` / `standard` / `strong`). A value that is not a known class is **not** silently treated as a literal model:
  - operator config (`model:` in reyn.yaml) keeps a backward-compatible literal passthrough (you may put `openai/gpt-4o` directly);
  - a **skill/op-supplied** model (`op.model`) that is not a known class is **rejected** and falls back to the runtime model (one warning), so a skill- or LLM-authored string cannot bypass the proxy config — the proxy config is the single source of truth for model selection.
- **Name position** (the *definition* of a model): the `model:` value inside a `models:` / `embedding.classes:` entry. A name should be `provider/model` (e.g. `openai/gpt-4o`, `sentence-transformers/all-MiniLM-L6-v2`). A bare name with no `/` is accepted (some LiteLLM strings are bare) but **warns** at load — add the prefix if resolution misroutes.

In one line: **a `_class` / tier position takes a class name (closed-world); a `model` position takes `provider/model` (validated). No position accepts both.**

### str form — literal (backward compatible)

If a str value **contains `/`**, it is treated as a literal LiteLLM model string:

```yaml
models:
  light:    gemini-flash-lite
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
  standard: gemini-flash-lite   # str form still OK alongside dict entries

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
| `reasoning_effort` | no | Reasoning budget for the model: `minimal` / `low` / `medium` / `high` / `disable` / `none`. **Validated at load** (see below). |
| `extends` | no | Inherit from a named class and deep-merge overrides (see below). |
| *(any other field)* | no | Silently passed through to litellm (passthrough policy). |

> **Cost limit**: use `max_completion_tokens`, not `max_tokens`.  `max_tokens` is a legacy
> soft hint that many providers ignore; it has no enforcement power on OpenAI o1+ or
> Anthropic models.  `max_completion_tokens` is enforced at the API level.

**Field policy**: `model` is the only required field. Most other fields are passed directly to `litellm.acompletion` without validation — unknown fields are silently forwarded (future-proof); typos cause silent litellm failures, not reyn errors. The one exception is `reasoning_effort`, which is validated at load (below).

### `reasoning_effort` (per-model reasoning budget)

Set how much the model is allowed to "think" before answering. Declared per model
definition so it's explicit and easy to understand:

```yaml
models:
  light:
    model: gemini-flash-lite
    reasoning_effort: low      # minimal | low | medium | high | disable | none
```

- **Valid values**: `minimal`, `low`, `medium`, `high`, `disable`, `none`. An invalid
  value **fails fast at config load** (a clear `ValueError` naming the bad value), not
  mid-call inside litellm.
- **Native mapping**: the value is passed through natively to litellm, which maps it to
  the provider's own reasoning budget. For Gemini (e.g. `gemini-2.5-flash-lite`):
  `low` → thinking budget 1024, `medium` → 2048, `high` → 4096, `minimal` →
  model-specific (512 for flash-lite), `disable` / `none` → 0. No hand-rolled
  `extra_body` needed.
- **Mutually exclusive with an `extra_body` thinking config**: `reasoning_effort` *is* the
  thinking-budget control, so declaring both `reasoning_effort` and an `extra_body`
  thinking config on the same model is **rejected at load** (pick one).
- **OpenAI summary opt-in (dict form)**: OpenAI reasoning models (o-series / GPT-5)
  do **not** return raw reasoning text — they encrypt the chain and expose only an
  optional *summary*, which is **opt-in**. For those models pass the dict form to
  request the summary text:
  ```yaml
  models:
    strong:
      model: openai/gpt-5
      reasoning_effort:
        effort: medium      # the budget level (validated, same set as above)
        summary: detailed   # opt into summary text → rides into reasoning_content
  ```
  litellm's GPT-5 transformation reads `{effort, summary}`. **Provider difference**:
  Gemini exposes raw reasoning text natively from the string form; OpenAI needs the
  dict + `summary` for any text (and even then it is a summary, not the raw chain).
  Without `summary`, an OpenAI model's `reasoning_effort` still controls the budget
  but no reasoning text is displayed.

> **Reasoning text IS captured, displayed, and replayed.** A non-zero
> `reasoning_effort` sets the provider's `includeThoughts=true`; reyn captures the
> reasoning text, displays it (TUI + chainlit, collapsible — `chat.reasoning.display`),
> and replays recent turns' reasoning into the next prompt (`chat.reasoning.continuity`).
> See the [`chat` block](#chat-block) for the toggles. (For OpenAI models the displayed
> text is the *summary* and only when the dict `summary` opt-in is set — see above.)

> **Known behavior — re-enables thinking on the tool-use path.** Reyn does not force
> thinking off; it relies on the provider default (off for Gemini 2.5). Setting
> `reasoning_effort` turns thinking on, including on the multi-turn tool-use path where
> Gemini previously had a parallel-tools + thinking interaction. Verify
> behavior on your model if you enable it for a tool-heavy agent.

> **Proxy passthrough (openai-compat).** When routing through a litellm proxy, reyn
> whitelists `reasoning_effort` via `allowed_openai_params` so it is forwarded to the
> proxy (which maps it to the provider's native thinking budget) instead of being
> rejected as an unsupported OpenAI param. No extra configuration needed.

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
| `gemini-flash-lite` | `gemini/gemini-2.5-flash-lite` | |
| `gemini-3.1-flash-preview` | `gemini/gemini-3.1-flash-preview` | |
| `gemini-2.0-flash` | `gemini/gemini-2.0-flash` | thinking disabled via `thinking_budget=0` |

User-declared entries **override** built-ins with the same name.  The built-in catalog
is a convenience starting point; your `reyn.yaml` is always the source of truth.

See [Reference: built-in models](../builtin-models.md) for per-entry details.

### `model_class_by_purpose` — per-purpose model class

Reyn makes several internal LLM calls beyond the main agent reply, each tied to a
logical **purpose**. By default every purpose uses your configured `model` (the
default class) — **routing follows the model you configured; there is no hidden
cheaper tier**. `model_class_by_purpose` lets you override the class for a
specific purpose; an unset purpose falls back to `model`.

| Purpose | What it covers |
|---|---|
| `router` | The per-turn chat router / intent classification. |
| `control_ir` | Control-IR sub-execution model. |
| `tool` | The default class for tool-spawned skill runs. |
| `compaction` | Context-compaction summarisation. |
| `judge` | Output-judging / evaluation calls. |

```yaml
model: standard                  # the default class for every purpose
models:
  standard: openai/gpt-5.4
  light:    openai/gpt-4o-mini
model_class_by_purpose:
  router: light                  # opt INTO a cheaper per-turn router (an explicit choice)
  # control_ir / tool / compaction / judge unset → follow `model` (gpt-5.4)
```

**Cost note**: the router runs on every turn, so the cheap-router optimisation is
still available — it is now an explicit one-line opt-in (`router: light`) rather
than a hidden default. Explicit per-call selections (a skill's `op.model`, a
phase's frontmatter `model_class`) still win over this fallback. Unknown purpose
keys are warned (not fatal) at load time.

## `llm` block

LLM-layer config: **`llm.router`** (opt-in litellm.Router) and
**`llm.retry`** (backoff timing for the Reyn self-retry layer).

```yaml
llm:
  router:
    use: false             # master switch (env REYN_LLM_USE_ROUTER is the fallback)
    num_retries: 3         # infra-exception retries (litellm Retry-After aware)
    fallbacks:             # primary model → ordered list of fallback models
      openai/gpt-4o-mini:
        - openai/gpt-3.5-turbo
    cooldown_time: 60      # seconds a deployment is cooled down after failures
    allowed_fails: 2       # failures before a deployment is cooled down
    credentials:           # credential rotation — multiple keys per model
      openai/gpt-4o-mini:  # ENV-VAR NAMES only; NEVER inline a key value
        - api_key_env: OPENAI_API_KEY_1
        - api_key_env: OPENAI_API_KEY_2
    retry_policy:          # per-exception-type retry counts (litellm.RetryPolicy)
      RateLimitErrorRetries: 5
      TimeoutErrorRetries: 3
  retry:
    jitter: true           # equal jitter (AWS pattern): sleep = base/2 + uniform(0, base/2)
    respect_retry_after: true  # honour provider Retry-After header (capped at max_backoff)
```

### `llm.router` fields

| Field | Type | Default | Meaning |
|---|---|---|---|
| `use` | bool | `false` | Master switch. `false` → direct `litellm.acompletion`. Supersedes `REYN_LLM_USE_ROUTER`. |
| `num_retries` | int | `3` | Infra-exception retry count (Retry-After aware). Supersedes `REYN_LLM_ROUTER_NUM_RETRIES`. |
| `fallbacks` | map | `{}` | `primary_model → [fallback_model, …]`. Empty → single-deployment Router (no chain). |
| `cooldown_time` | float\|null | `null` | Seconds a deployment is cooled down after `allowed_fails` failures. Only meaningful with a fallback chain. |
| `allowed_fails` | int\|null | `null` | Failures before a deployment is cooled down. |
| `credentials` | map | `{}` | Credential rotation: `model → [{api_key_env: ENV_VAR_NAME}]`. Each usable key → one Router deployment (same model) → the Router rotates / fails over across keys. **Reference env-var NAMES only — never inline a key value**; values are read from `os.environ` at build time and are never logged or cache-fingerprinted. A declared model whose env vars all resolve to nothing is a load error (no silent keyless deployment). |
| `retry_policy` | map\|null | `null` | Per-exception-type retry counts. Absent (null) → litellm defaults (`num_retries` applies uniformly). When set, constructs a `litellm.RetryPolicy` and passes it to the Router. Supported keys: `RateLimitErrorRetries`, `TimeoutErrorRetries`, `BadRequestErrorRetries`, `AuthenticationErrorRetries`, `ContentPolicyViolationErrorRetries`, `InternalServerErrorRetries`. |

On the Router path, retry count is **config-only**: `num_retries` is taken from
`llm.router.num_retries` (a per-call `max_retries` is not applied), so the retry
budget has a single source. (On the direct, non-Router path the per-call
`max_retries` is unchanged.)

### `llm.retry` fields

Controls the **timing** of the Reyn self-retry layer only (semantic-retry
behaviours — EmptyLLMResponseError, empty\_stop\_retry, compaction shrink — are
unaffected). Both defaults are `true`.

| Field | Type | Default | Meaning |
|---|---|---|---|
| `jitter` | bool | `true` | Apply equal jitter (AWS pattern): `sleep = base/2 + uniform(0, base/2)` where `base = min(base_s * 2**attempt, max_backoff)`. Range `[base/2, base]`. Prevents thundering herd when parallel chains retry in lockstep. `false` → pure exponential (2 s, 4 s, 8 s, 16 s). |
| `respect_retry_after` | bool | `true` | When a retryable exception carries a `Retry-After` header (delta-seconds **or** HTTP-date), honour it (capped at `_LLM_RETRY_MAX_BACKOFF_S` = 16 s) **instead of** the jittered backoff. Falls back to jittered backoff when the header is absent or unparseable. `false` → always use jittered backoff. |

> **Router path**: when `llm.router.use: true`, the litellm.Router owns
> infra-exception retry with its own `Retry-After` respect. The `llm.retry`
> fields only apply to the Reyn self-retry layer (= the direct, non-Router path,
> plus `EmptyLLMResponseError` on both paths). See the `llm.router` block above.

## `chat` block

Chat-session runtime knobs. `chat.compaction` controls chat-history compaction
(ratio-based budget; see `reyn.local.yaml.example`). `chat.reasoning` controls
model reasoning/"thinking" text handling.

```yaml
chat:
  reasoning:
    continuity: true      # persist reasoning to history + replay recent turns
    display: true         # show reasoning in the UI (TUI + chainlit, collapsible)
    recent_turns: 3       # turns of reasoning to replay; <=0 = unbounded
```

### `chat.reasoning` fields

Capture of the provider `reasoning_content` is **always-on**; these knobs gate
what happens afterwards. Both `continuity` and `display` default **on**.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `continuity` | bool | `true` | Persist reasoning to history **and** replay the recent turns' reasoning into the next turn's system prompt (cross-user-turn reasoning continuity, a text-section mirroring `act_turn_reasoning`). Opt-out to disable persist + replay. |
| `display` | bool | `true` | Surface reasoning in the UI (TUI + chainlit, collapsible). Opt-out to hide it. Independent of `continuity`. |
| `recent_turns` | int | `3` | How many recent turns' reasoning to replay under `continuity`. `<= 0` (e.g. `0` / `-1`) = unbounded (keep all). Bounding matters on Gemini — there is no provider auto-filter, so reasoning accumulates and is billed in full. |

> **Provider note**: on the Gemini-via-proxy path the reasoning is replayed as a
> text section (the model sees it in-prompt), and `reasoning_content` is stripped
> from the wire-shape assistant messages to avoid a double-inject (litellm's
> vertex transformation would otherwise emit it natively too). Anthropic/DeepSeek
> direct-API require the native `reasoning_content` round-trip on the tool-use
> path; litellm auto-manages that when it's left on the wire — a known
> provider-dependency, not implemented here (proxy + Gemini reality).

## `safety` block

Unified stop-condition namespace. Each value can be overridden per-invocation by the matching CLI flag. (The old top-level `limits:` key is gone; `safety:` is the single source of truth.)

```yaml
safety:
  loop:
    max_router_calls_per_turn: 3 # chat-router calls per user turn
    max_router_iterations: 5   # LLM tool-call iterations per user turn (CLI --max-iterations overrides)
    max_tool_calls_per_turn: 50 # max tool_calls honoured from ONE completion (cost-bound); 0 = unlimited
    max_hook_driven_turns: 25  # loop valve: cap hook self-continuation; resets on user turn; 0 = unlimited
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
  threat_scan:
    enabled: true              # content-layer prompt-injection scan + fence
    fail_open: true            # scanner error → allow (FN tolerated over FP)
    fence_enabled: true        # structurally fence untrusted content as data
    block_severity: block      # min severity that blocks at write seams: block | warn
    custom_patterns: []        # operator [regex, id, scope, severity] extensions
  spawn:
    max_depth: 10                     # max LLM spawn-lineage chain depth (agent_spawn); 0 = unlimited
    max_children: 20                  # max fan-out: direct children per parent AND topology size; 0 = unlimited
    max_pipeline_fan_out_depth: 5     # max pipeline for_each fan-out NESTING depth; 0 = unlimited
    max_pipeline_spawns: 100          # max ephemeral sessions ONE pipeline run may spawn; 0 = unlimited
```

### `safety.loop` fields

| Path | Type | Default | CLI flag | Description |
|------|------|---------|----------|-------------|
| `safety.loop.max_router_calls_per_turn` | int | `3` | — | Chat-router invocations per user turn. `0` = unlimited. |
| `safety.loop.max_router_iterations` | int | `5` | `--max-iterations` | Maximum LLM tool-call iterations per user turn. CLI `--max-iterations` overrides when provided; `reyn run-once` uses CLI default of 80. |
| `safety.loop.max_tool_calls_per_turn` | int | `50` | — | Cost-bound: maximum `tool_calls` honoured from a SINGLE LLM completion. A degenerate completion can emit thousands (observed 3451); the OS processes only the first N, drops the overflow, and appends a re-grounding notice. `0` = unlimited. |
| `safety.loop.max_hook_driven_turns` | int | `25` | — | Loop valve: caps hook self-continuation. Each hook-originated (`kind="hook"`) turn counts 1; the counter resets on each human user turn. When the count would exceed the cap the next hook turn hits the `safety.on_limit` checkpoint (warn → ask_user → abort) instead of running — a backstop that does not obstruct intentional loop-engineering. `0` = unlimited. |
| `safety.loop.max_agent_hops` | int | `3` | — | Maximum delegation depth (user → A → B → C = 3 hops). |

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

### `safety.threat_scan` fields

Content-layer threat defense: inspects untrusted content for prompt-injection before it enters the system prompt / context, complementing the execution layer (permissions / sandbox). Defense-in-depth = a structural **fence** (mark untrusted content as data) plus a pattern **scan** backstop.

| Path | Type | Default | Description |
|------|------|---------|-------------|
| `safety.threat_scan.enabled` | bool | `true` | Master switch. Default-on: content→context (read) seams detect non-blocking + emit telemetry; agent-write seams block. |
| `safety.threat_scan.fail_open` | bool | `true` | Scanner error → allow (a false-negative is tolerated over a false-positive that would wedge a turn). |
| `safety.threat_scan.fence_enabled` | bool | `true` | Structurally fence untrusted content (random-id markers + control-token strip + unicode normalization) so the LLM treats it as data, not instructions. For *which* content this applies to, see [Security: what gets structurally fenced](../../concepts/agent-engineering/security.md#what-gets-structurally-fenced). |
| `safety.threat_scan.block_severity` | string | `block` | Minimum severity that BLOCKS at agent-write seams (memory write / skill install). `block` = only `block`-severity patterns; `warn` = warn-severity also blocks (stricter). |
| `safety.threat_scan.custom_patterns` | list | `[]` | Operator pattern extensions, each `[regex, id, scope, severity]`. Merged into the built-in catalog for scans. |

### `safety.spawn` fields

Operator bounds on the LLM spawn tree — a DoS guard so an agent cannot mint an unbounded org. Set in `reyn.yaml` (the restart-only OUT layer): an LLM has no runtime path to raise its own base limit. Enforced at the LLM spawn **seams** (`agent_spawn`, `topology_create`); the operator CLI create path is unbounded (authority). Defense-by-default (non-zero) — there is no backward-compat spawn tree to break.

When a spawn would exceed a limit, the `safety.on_limit` checkpoint fires — the same mode-driven framework used by loop and budget caps:

- **`interactive`** (default): prompts the operator for approval to extend. On approval, the extension is recorded per-spawner so the same scope does not re-prompt. The base config limit is unchanged; any extension is operator-approved, never LLM-driven.
- **`unattended`**: rejects immediately (no prompt possible — CI / scripted runs).
- **`auto_extend`**: auto-approves up to `auto_extend_times` extensions, then rejects.

`max_depth` and `max_children` carry **separate per-spawner extension keys** so an operator-approved increase in one does not silently widen the other.

| Path | Type | Default | Description |
|------|------|---------|-------------|
| `safety.spawn.max_depth` | int | `10` | Maximum spawn-lineage chain depth (operator-top = 0; each `agent_spawn` +1). Exceeding this fires the `safety.on_limit` checkpoint. `0` = unlimited. |
| `safety.spawn.max_children` | int | `20` | Maximum fan-out: governs BOTH the direct spawn-children per parent (`agent_spawn`) AND the member count of a `topology_create`d topology (org size). Exceeding this fires the `safety.on_limit` checkpoint. `0` = unlimited. |
| `safety.spawn.max_pipeline_fan_out_depth` | int | `5` | Pipeline fan-out NESTING bound: the max depth of nested `for_each` scopes (a top-level `for_each` = 1; a `for_each` inside another's `do`/`collect` = 2; …). A `for_each` exceeding this FAILS the step (bounded-by-construction; no `on_limit` prompt — pipeline runs are non-interactive). Distinct from `max_depth` (spawn lineage): a pipeline agent-step carries no lineage, so `max_depth` does not cover fan-out. `0` = unlimited. |
| `safety.spawn.max_pipeline_spawns` | int | `100` | Pipeline spawn-COUNT bound: the max ephemeral sessions ONE pipeline run may spawn across all its `agent` steps (top-level or fanned out via `for_each`). A per-run monotonic counter; the spawn past the cap FAILS the step. The ONLY spawn-count enforcement for lineage-less pipeline agent-steps (`max_children` does not cover them). `0` = unlimited. |

See [`safety.on_limit` fields](#safetyonlimit-fields) for the mode settings.

## `tool_use` block

Per-layer tool-use scheme selector. Each layer picks a registered `ToolUseScheme` by name — a pluggable, per-layer mechanism for how tools are presented to and dispatched from the LLM.

```yaml
tool_use:
  chat: enumerate-all         # default
  step: universal-category    # default
  phase: universal-category   # default
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `chat` | string | `enumerate-all` | Tool-use scheme for the top-level chat layer. **Default `enumerate-all`** — flat-lists actions so the LLM invokes them directly instead of hallucinating `invoke_action` names (raised non-hot-list tool-use ~30%→100%). Set to `universal-category` for a minimal-surface / many-tool catalog (discover-then-call), or another registered scheme. |
| `step` | string | `universal-category` | Tool-use scheme for the skill step layer. |
| `phase` | string | `universal-category` | Tool-use scheme for the OS phase layer. |

The chat layer defaults to `enumerate-all`; `step` / `phase` keep `universal-category`. A scheme owns how the `tools=` payload is built, the SP tool-use instructions, how an LLM response is interpreted, and how it is dispatched — so swapping a layer's scheme changes the whole tool-use loop for that layer without OS changes. `universal-category` remains available per-layer via this config (e.g. for very large tool catalogs where flat-listing every action would bloat the request). `retrieval` (search-over-tools) and `CodeAct` are likewise supported opt-in schemes per layer; `retrieval` additionally requires `action_retrieval.embedding_class` set to a configured embedding provider.

For what each scheme does and **when to choose which** (`enumerate-all` / `retrieval` / `CodeAct` vs the default), see [Tool-Use Schemes](../../concepts/tools-integrations/tool-use-schemes.md).

## `phase` block

Per-phase runtime settings.

```yaml
phase:
  act_results_compaction:
    recent_act_turns_raw: 5
    control_ir_results_ratio: 0.50
    summarize_older_threshold_tokens: null
    use_chars4_estimate: false
```

### `phase.act_results_compaction` fields

Controls how the act-loop's accumulated `control_ir_results` are compacted when they approach the context budget. Sibling to `chat.compaction` (conversation history).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `recent_act_turns_raw` | int | `5` | Keep the last N act-turn results verbatim; compact older ones. Set higher than the conversation-history default because phase ops carry specific structured data (paths, line numbers, exit codes) the LLM needs for planning next ops. |
| `control_ir_results_ratio` | float | `0.50` | Fraction of `main_pool` (= `T_max - T_SP`) allocated for the `control_ir_results` portion of the act-loop context. Sibling to `chat.compaction.component_weights["body"]`. |
| `summarize_older_threshold_tokens` | int \| null | `null` | Total token threshold above which older results are compacted. `null` derives the threshold from `control_ir_results_ratio × main_pool` (via `ComputedBudgets`). |
| `use_chars4_estimate` | bool | `false` | When `true`, use `len(text)//4` for token estimation (latency opt-out). |

## `web` block

SSL settings for `web_fetch` and the MCP package registry.

```yaml
web:
  fetch:
    verify_ssl: true     # true | false | omit (default: env-var chain)
    ca_bundle: /path/to/ca-bundle.pem   # optional custom CA bundle
    max_download_bytes: 10485760        # wire-byte ceiling (default 10MB)
    allow_private_ips: false            # SSRF: opt-in to private IPs (default deny)
  ws_max_size: 16777216                 # WS inbound-frame ceiling (default 16MB)
```

Priority chain (highest first):

| Priority | Condition | Effective SSL config |
|----------|-----------|----------------------|
| 1 | `web.fetch.ca_bundle` set | Custom CA bundle file (`verify=<path>`) |
| 2 | `web.fetch.verify_ssl: false` | Disable SSL verification (`verify=False`) — **use only in controlled environments** |
| 3 | `web.fetch.verify_ssl: true` | Force SSL verification (`verify=True`) |
| 4 | Both unset | Fall through: `SSL_VERIFY` env var → `litellm.ssl_verify` → `SSL_CERT_FILE` → `True` |

`verify_ssl` and `ca_bundle` also apply to MCP registry HTTP calls (package install).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `web.fetch.max_download_bytes` | int | `10485760` (10MB) | Maximum response bytes `web_fetch` reads off the wire. A response whose `Content-Length` exceeds this is rejected before any body is downloaded; a chunked / unknown-length body is aborted once the stream passes the ceiling (status `too_large`). Guards against an unbounded-body memory blow-up from a hostile or runaway URL. `<= 0` or non-integer falls back to the default. |
| `web.fetch.allow_private_ips` | bool | `false` | SSRF opt-in. When `true`, `web_fetch` / `safe.http` may fetch **private** RFC1918/ULA addresses (enterprise internal-fetch). Link-local, cloud-metadata (`169.254.169.254`), and loopback are **always** denied regardless of this flag. HTTP redirects are re-validated per hop (both the host allowlist and the IP-deny), so an allowlisted host cannot redirect to an internal target. Also exported to the `REYN_FETCH_ALLOW_PRIVATE_IPS` env var so the safe.http subprocess and registry clients honor the same opt-in. |
| `web.ws_max_size` | int | `16777216` (16MB) | Maximum size (bytes) of a single inbound WebSocket frame the `reyn web` gateway accepts; a larger frame is rejected by the server before delivery. Pins the WebSocket frame ceiling explicitly instead of relying on the server library's implicit default, so the bound stays in place across server-library upgrades. Operators may tighten or loosen it. `<= 0` or non-integer falls back to the default. |

## `eval` block

Trace exporter backends. When configured, reyn exports event traces from every skill run to the listed backends.

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

## `hooks` block

Agent-lifecycle hooks — a thin operator layer over the unified inbox
and the P6 lifecycle. A **list** of entries; each fires at a lifecycle point (`on`)
and carries **exactly one** of three mutually-exclusive schemes:

- **`template_push`** — inject an attributed `[hook:<name>]` message from a config
  Jinja2 template.
- **`shell_exec`** — run an external command as a pure side-effect (output IGNORED).
- **`shell_push`** — run a command whose **stdout is a JSON push-directive**, pushed
  via the same path as `template_push` (the only difference is the directive's
  source: captured stdout vs a Jinja2 render).

Hooks never silently mutate tool results; pushes are new, attributed, evented
messages.

```yaml
hooks:
  - name: next_step              # optional → the [hook:next_step] attribution (absent → the point)
    on: turn_end                 # turn_start|turn_end|session_start|session_end|task_start|task_end
    template_push:
      message: "Turn complete — consider the next step."
      wake: false                # false = passive context (C); true = start a turn (E)
      push_when: "true"          # optional Jinja2 → bool; false skips the push
  - on: session_start
    shell_exec: "echo session-started >> /tmp/reyn-hooks.log"
  - name: dynamic                # stdout decides whether/what/how to push
    on: turn_end
    shell_push: "scripts/decide-next.sh"   # emits {"push_when":true,"wake":true,"message":"..."}
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `on` | string | _required_ | Lifecycle point: `turn_start`, `turn_end`, `session_start`, `session_end`, `task_start`, `task_end`. |
| `name` | string | _the point_ | Optional operator label surfaced as the `[hook:<name>]` attribution prefix on a push. Absent → defaults to the hook-point (e.g. `[hook:turn_end]`). |
| `template_push` | map | _none_ | Inbox-push hook from a Jinja2 template (one of the three schemes). `message` (Jinja2 → text), `wake` (bool/Jinja2, default `true`: `true` starts a new turn = self-continuation; `false` rides along with the next turn as passive context), `push_when` (Jinja2 → bool, default `true`; `false` skips), `session` (parsed + carried forward-compat; cross-session routing is **not yet wired** — a no-op today, tracked follow-up). |
| `shell_exec` | string | _none_ | A shell command run as a pure side-effect (one of the three schemes). Sandbox-gated + consent-allowlisted; stdout/stderr are logs, never parsed. |
| `shell_push` | string | _none_ | A shell command whose **stdout is a single JSON object** `{"push_when": bool, "wake": bool, "message": str, "session"?: str}` (first three required), pushed via the same path as `template_push`. stdout must be pure JSON (logs → stderr). Sandbox-gated + consent-allowlisted. Any failure (non-zero exit, invalid JSON, missing/wrong-typed field) skips the push (fail-safe). |

## `sandbox` block

Backend selection, unsupported-platform policy, and the agent-level sandbox
policy for `sandboxed_exec` ops + the OS's in-process file/http gates.

```yaml
sandbox:
  backend: auto          # auto | seatbelt | landlock | noop
  on_unsupported: warn   # warn | error | ignore
  policy:                # optional — the agent-level (operator) sandbox policy
    network: true
    read_paths: ["/"]
    write_paths: ["/"]
    allow_subprocess: true
    env_passthrough: ["PATH", "HOME"]
    timeout_seconds: 600
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `auto` | Enforcement backend. `auto` lets the OS pick: macOS < 26 → `seatbelt` (sandbox-exec SBPL), Linux ≥ 5.13 with `sandbox-linux` extra → `landlock` (+ optional seccomp-BPF), otherwise → `noop` (audit-only, no enforcement). Explicit values force a specific backend. |
| `on_unsupported` | string | `warn` | Policy when **no OS sandbox backend is available** — whether an explicit `backend` was forced-but-unavailable OR `backend: auto` found no platform backend (the auto path now honors this too). `warn` logs a WARNING at selection and falls back to `noop` (default — not silent). `error` raises `RuntimeError` (**fail-closed** — refuse to run AI-generated code unsandboxed; set this where enforcement is required, and it now works with the default `backend: auto`). `ignore` silently falls back. |
| `policy` | map | _none_ | **Agent-level (operator) sandbox policy.** When set, it is the deterministic policy applied to sandboxed ops **and** folded into the `SandboxLayer` of the permission intersection (`∩`) for the OS's in-process file/http gates — **winning over** op-declared fields, so a skill or the LLM cannot widen it. Omitted (the default) means **no agent-level restriction**: the `SandboxLayer` stays the identity (`⊤`) and op-level fields govern, exactly as before. Sandbox authorization is an operator/run concern. See sub-keys below. |

### `sandbox.policy` sub-keys

When `sandbox.policy` is present, these mirror the `SandboxPolicy` fields. Unknown keys are rejected at config load.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `network` | bool | `false` | Allow outbound network from the sandboxed process. The primary exfiltration gate. |
| `write_paths` | list[string] | `[]` | Filesystem paths the process may write (tight guard). Write implies read for these paths. |
| `read_deny_paths` | list[string] | `[]` | Sensitive paths to DENY from the broad read surface (defense-in-depth). Enforced only on backends that support deny-after-allow rules (Seatbelt); not enforceable on allowlist-only backends (Landlock). |
| `read_paths` | list[string] | `[]` | **Legacy.** Formerly the strict read allowlist. Reads are broad by default under the current scoping model; this field now documents intended read targets only. |
| `allow_subprocess` | bool | `false` | Whether the process may spawn children. Advisory under Seatbelt. |
| `env_passthrough` | list[string] | `[]` | Env-var names that pass through to the sandboxed process. `PATH` is always passed through. |
| `timeout_seconds` | int | `60` | Wall-clock cap enforced by the backend. |

See [Reference: control-ir — `sandboxed_exec`](../runtime/control-ir.md#sandboxed_exec) for the op schema and backend selection details.

## `action_retrieval` block

Universal catalog visibility + retrieval settings.  Provides the chat router with **universal catalog wrappers** (`list_actions` / `describe_action` / `invoke_action`) for uniform browse / describe / invoke across all skill / agent / MCP / file / memory / RAG categories.  On by default — operators who want the prior `tools=` shape can opt out with `universal_wrappers_enabled: false`.

```yaml
action_retrieval:
  universal_wrappers_enabled: true    # default; set false to opt out
  embedding_class: local-mini         # default; null disables search_actions
  hot_list_n: 0                       # 0 = off (default); set e.g. 10 to opt in
  mode: default                       # default | minimal | performance
```

### `action_retrieval` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `universal_wrappers_enabled` | bool | `true` | When `true` (default), the router's `tools=` exposes only the 4 universal wrappers (`list_actions`, `search_actions`, `describe_action`, `invoke_action`) plus hot-list direct aliases.  Legacy per-kind tools (`invoke_skill`, `call_mcp_tool`, etc.) are no longer surfaced to the LLM but remain available as wrapper backing handlers.  `search_actions` is gated separately by `embedding_class`.  Set `false` to disable the wrapper surface entirely (= legacy tools become the only addressing path again). |
| `embedding_class` | string \| null | `"local-mini"` | Name of an entry in [`embedding.classes`](../../concepts/data-retrieval/rag.md) to use for action-retrieval semantic search.  Default `local-mini` (= `sentence-transformers/all-MiniLM-L6-v2`).  When `null` or empty, `search_actions` is excluded from `tools=` even when wrappers are enabled.  Setting this also enables eager embedding build on cold-start sessions to avoid first-turn hallucinations.  **Graceful degrade**: if the chosen class points at a `sentence-transformers/` model but the `local-embed` extras aren't installed, reyn silently treats this as `null` and `list_actions` surfaces the install command to the LLM. Set explicitly to `standard` (= OpenAI) or `null` (= opt out) to override. |
| `hot_list_n` | int | `0` | Hot-list projection size for top-N `freq+recency` direct aliases. `0` (default) disables hot-list entirely — `list_actions` is the canonical discovery path. Set to `10` or higher to opt in; the seed, usage tracker, and alias-builder remain fully operative. |
| `mode` | string | `"default"` | Operational mode label: `"minimal"` (max cache stability, no hot list) / `"default"` (balanced) / `"performance"` (large hot list).  Free-form string; callers layer semantics on top. |
| `hot_list_seed` | list \| string | `"default"` | Seed for the hot-list projection. `"default"` uses the built-in freq+recency seeding; a list of qualified action names (e.g. `["mcp__call_tool"]`) pins those as the initial hot list before usage stats accumulate. |

### Quick-start — opt out

```yaml
# reyn.yaml — preserve the legacy tools= shape
action_retrieval:
  universal_wrappers_enabled: false
```

When enabled (default), the chat router's `tools=` includes the wrappers at the tail.  The LLM can call:

- `list_actions(category=["mcp"])` → enumerate available actions in a category as qualified names (e.g. `mcp__call_tool`)
- `describe_action(action_name="mcp__call_tool")` → fetch the input schema
- `invoke_action(action_name="mcp__call_tool", args={...})` → execute via the existing handler

Resource categories (`mcp.server`, `rag_corpus`, `memory_entry`, …) also support `invoke_action`.  Unknown action names return a structured error with `suggestions` ranked by string similarity, so the LLM recovers in one turn.

See Concepts: architecture (architecture doc removed) for the tool registry / dispatch background.

## `agent` block

Runtime agent identity for audit trail and HTTP header propagation.

```yaml
agent:
  id: "reyn/acme/code-review-agent"  # default: reyn/<hostname>
```

### `agent` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `agent.id` | string | `reyn/<hostname>` | Stable identifier for this Reyn instance. Stamped onto every P6 event payload as `agent_id` and injected into outgoing MCP, A2A, and external HTTP requests as the `X-Reyn-Agent-Id` header (SOC2 / ISO27001 / METI v1.1 audit pattern). Recommended format: `reyn/<org>/<role>` (operator-defined). An empty string falls back to the default so leaving the field blank does not emit an empty `agent_id` into events or headers. |

The default `reyn/<hostname>` gives a fresh install a usable identity without operator action. Override in `reyn.yaml` when running multi-agent fleets or enterprise deployments that need a stable per-role identifier.

See [Concepts: multi-agent — Agent ID propagation](../../concepts/multi-agent/multi-agent.md) for cross-agent tracing and A2A header forwarding.

## `delegation` block

Cross-agent delegation policy (#2081). Selects the capability floor a **delegated** agent — one spawned by another agent's delegation, recursively — receives when it is otherwise unbound by a topology `capability_profile`.

```yaml
delegation:
  capability_default: inherit   # inherit (default) | deny
```

### `delegation` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `delegation.capability_default` | `inherit` \| `deny` | `inherit` | `inherit` — a delegate inherits the spawner's capability surface (no extra narrowing; byte-identical to pre-#2081). `deny` — an **unbound** delegate is narrowed by the built-in restrictive `_delegate` profile (dangerous-tool classes denied: re-delegation, side-effect execution, memory-writes, MCP install) unless a topology `capability_profile` binding re-grants it (the binding **replaces** the default — composition is most-restrictive-wins and cannot re-grant). The default-deny propagates **recursively**: a sub-delegate is itself a delegate, so a re-granted coordinator's own sub-delegates are still default-denied (no laundering). |

Only the unbound-delegate fallback is affected. A top-level agent and any topology-bound agent are unchanged. The restrictive floor reuses the same single-sourced dangerous-tool taxonomy as the `_untrusted` content-narrowing profile; operators may tune it independently via `.reyn/capability_profiles/_delegate.yaml`.

See [Concepts: multi-agent](../../concepts/multi-agent/multi-agent.md) and [Concepts: capability profile](../../concepts/runtime/capability-profile.md).

## `auth` block

OAuth provider configurations for `reyn auth login`. Each named entry under `auth.providers` defines an RFC 8628 Device Authorization Grant provider. Empty by default; the operator declares providers they want to authenticate against.

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

`${secret:<key>}` values resolve at config-load time from `~/.reyn/secrets.env`. Use `reyn secret set <key>` to store them.

See also:

- [Reference: `reyn auth`](../../reference/cli/auth.md) — `reyn auth login/list/revoke` commands
- [Concepts: secret handling](../../concepts/runtime/secret-handling.md) — OAuth lifecycle and credential scoping
- [Concepts: multi-agent](../../concepts/multi-agent/multi-agent.md) — agent identity propagation

## `cron:` block

Schedule recurring message dispatches. The scheduler runs as part of
`reyn web` (= started in the FastAPI lifespan) or as a foreground
process via `reyn cron run`.

```yaml
cron:
  jobs:
    - name: morning_news
      to: news_agent            # target agent name
      message: "今日の主要ニュースをまとめて"
      schedule: "0 9 * * *"     # every day 09:00
      enabled: true

    - name: weekly_ops_report
      to: ops_agent
      message: "weekly ops report"
      schedule: "0 9 * * MON"   # Monday 09:00
      enabled: true
```

### Fields

- **`name`** (required) — job identifier, unique within the schedule
- **`to`** (required) — target agent name; the message is dispatched to its
  inbox with `sender="cron:<name>"` attribution
- **`message`** (required) — free-form text delivered to the target agent
- **`schedule`** (required) — 5-field cron expression
  (minute / hour / day-of-month / month / day-of-week)
- **`notify`** (optional) — opt-in unattended notification channel
- **`input`** (optional, default `{}`) — extra input dict carried on the job
- **`enabled`** (optional, default `true`) — `false` keeps the entry in
  configuration but skips scheduling

> Legacy skill-based jobs (a bare `skill` name) are no longer supported — the
> skill runtime was removed. An old on-disk `cron.yaml` carrying such an entry
> is warned-and-skipped at load, not rejected.

### Cross-references

- `docs/reference/cli/cron.md` — `reyn cron run/list/status`
- `docs/concepts/data-retrieval/operational-intelligence.md` — scheduling a
  recurring events-log indexing agent

## `fs_watch:` block

Operator-declared filesystem watch paths (#2608 H4). Each path is watched
recursively; a create/modify/delete under it fires the `file_changed`
external-event hook (see the `hooks:` block above — `on: file_changed`, plus
a `matcher: {path: "..."}` glob to scope a hook to a sub-tree).

```yaml
fs_watch:
  paths:
    - /repo/src
    - /repo/docs
  debounce_seconds: 0.2   # coalesce a write-burst on one path into ONE fire
```

### Fields

- **`paths`** (optional, default `[]`) — list of directories to watch,
  recursively. Empty (the default) → the watcher never starts.
- **`debounce_seconds`** (optional, default `0.2`) — a burst of writes to the
  SAME path within this window coalesces to one hook fire.

### Security

`fs_watch:` is **OUT-set only** — restart-only, loaded from
`reyn.yaml`/`reyn.local.yaml`, never from a `.reyn/*.yaml` hot-reload file.
There is no op or tool verb an agent can use to register or widen a watch —
a filesystem-wide change-notification feed is an info-gathering surface, so
it gets the same operator-only gate as `sandbox:` policy.

### Requirements

Requires the optional `watchdog` package (`pip install reyn[fs-watch]`). If
`paths` is configured but `watchdog` isn't installed, the feature logs a
warning and stays off — the rest of the session is unaffected.

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

### MCP install

The legacy `permissions.mcp_install: ask | allow | deny` bool axis was removed. MCP install is now gated by the same list axes the rest of the OS uses:

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

Both the async op-handler client (`reyn.core.registry.client`) and the safe-mode skill-internal lookup (`reyn.api.safe.mcp.registry`) iterate the list in order:

- `lookup(server_id)` returns the first non-404 hit; all 404 → `None`.
- `search(query)` returns the first non-empty result list; all empty → `[]`.

This implements "private first, public fallback" semantics. Legacy singular `REYN_MCP_REGISTRY_URL` is honored as a one-item list for backward compat.

See [Concepts: permission model](../../concepts/runtime/permission-model.md) → "Collapse arc" for the full migration story and the canonical decomposition table.

> Legacy `permissions.mcp_install` keys in older `reyn.yaml` files are accepted with a `DeprecationWarning` and translate to the equivalent `file.write` / `http.get` gates during the migration window.

The full permission grammar is documented in `reference/config/permissions.md`.

## `${VAR}` interpolation {#var-interpolation}

Any string field in any section of `reyn.yaml` (or `reyn.local.yaml` / `~/.reyn/config.yaml`) can reference an environment variable using `${VAR}` syntax. Variables are resolved from `os.environ` at startup, after `~/.reyn/secrets.env` is loaded into the environment (see [Concepts: secret handling](../../concepts/runtime/secret-handling.md)).

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

For each setting, reyn merges these sources, lowest priority first — later layers override earlier:

1. **Built-in defaults** — the values shipped with reyn (e.g. `model: standard`).
2. `~/.reyn/config.yaml` — user-global.
3. `reyn.yaml` — project, committed.
4. `reyn.local.yaml` — project, gitignored (machine-local overrides + values written by `reyn config set`).
5. `<project>/.reyn/mcp.yaml` — the dynamic MCP server registry. Merged **last for the `mcp.servers` section**, so servers added by `reyn mcp install` override any `mcp.servers` you hand-edit in `reyn.yaml` / `reyn.local.yaml`.
6. `<project>/.reyn/cron.yaml` — the dynamic cron registry. Merged **last for the `cron.jobs` section**, so jobs registered at runtime override `cron.jobs` in `reyn.yaml` on a name collision.
7. CLI flags — applied last, per invocation.

Layers 5 and 6 are scoped: each carries only its own section (`mcp.servers` / `cron.jobs`) and is merged section-by-section, so it never touches unrelated settings. `${VAR}` interpolation is applied once after all YAML layers are merged, before CLI flags.

> **Why `.reyn/mcp.yaml` and `.reyn/cron.yaml` win**: these are the runtime-mutable registries (written by `reyn mcp install` and runtime cron registration) rather than the edit-and-restart static files. Putting them last means a freshly installed server or registered job is the effective entry without the operator also having to touch `reyn.yaml`.

`<project>/.reyn/config.yaml` is no longer loaded — it is a deprecated general-config file, not the active `.reyn/mcp.yaml` / `.reyn/cron.yaml` registries above. If it still exists on disk, reyn prints a warning and skips it. Move its contents to `reyn.local.yaml`, then delete it.

## `cost` block

Budget caps and rate limits. All fields are optional; omitting a field (or setting its `hard_limit` to `null`) means **unlimited**.

Each token / cost cap (`per_agent_tokens`, `per_agent_cost_usd`, `daily_*`, `monthly_*`) is a `CostLimitConfig` with three sub-fields: `hard_limit` (the cap; `null` = unlimited), `warn_ratio` (warn threshold as a fraction of `hard_limit`, default `0.8`), and `extension_calls` (per-grant extension amount; `> 0` opts the dimension into the unified `safety.on_limit` flow, whose ask-vs-auto-extend-vs-deny behaviour follows `safety.on_limit.mode`). The examples below set only the commonly-tuned `hard_limit` / `warn_ratio`; `extension_calls` defaults to `0` (hard-refuse on hit). The per-dimension `ask_on_exceed` bool was removed (subsumed into `safety.on_limit.mode`).

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

  # Daily / monthly quota (persistent across process restarts)
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

> **Note**: The router call cap (`max_router_calls_per_turn`) lives under `safety.loop`. See the [`safety` block](#safety-block) above.

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

## `cost_warn` block

High-cost model pre-selection awareness. Surfaces a `[⚠ high-cost model: …]` marker in the conversation pane when the resolved model's input cost per 1M tokens exceeds the configured threshold. Fires at `/model <class>` switch and once at session startup. De-duped per session — the same model class is warned at most once per session. Orthogonal to the [`cost` block](#cost-block) (= cumulative spend caps) and `ContextBudgetAdvisor` (= per-turn token ceiling).

```yaml
cost_warn:
  enabled: true
  model_threshold_per_1m_input_usd: 5.0  # warn above $5/1M input tokens
  block_on_high_cost: false              # optional confirm gate (see below)
```

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `true` | Master switch. Set to `false` to silence all model-cost warnings. |
| `model_threshold_per_1m_input_usd` | float | `5.0` | Warn when the selected model's input rate exceeds this value (USD per 1M tokens). Default catches Opus-class (~$15/1M) without triggering on Sonnet-class (~$3/1M). |
| `block_on_high_cost` | bool | `false` | When `true`, a `/model <class>` switch to a high-cost model is held for an interactive confirmation and applies **only on approval** (routed through the shared safety-limit framework, the same one budget-exceed continuation uses). A decline leaves the current model unchanged. A non-interactive session (no TTY) **fail-closes** — it cannot show the confirm, so the high-cost switch is denied; keep this `false` to use high-cost models head-less. Session startup stays warn-only regardless of this flag. |

**Pricing source:** reyn looks up model costs from the [LiteLLM pricing database](https://github.com/BerriAI/litellm) (`litellm.model_cost`). Models not in the database are treated as below-threshold (no warning). Custom or proxy models that resolve to a key in the database will be matched.

## MCP servers

External tool servers reyn can call via the [Model Context Protocol](../../concepts/tools-integrations/mcp.md). Each entry under `mcp.servers:` is keyed by a short name (the same name the skill declares in `permissions.mcp` and emits in `mcp` ops).

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

> **Note**: internally this is the `ReynConfig.mcp_search_threshold` field, but the operator-facing key is `mcp.search_threshold` (read from the `mcp:` block) — set it there, not as a top-level `mcp_search_threshold:`.

```yaml
mcp:
  search_threshold: 30   # default; set 0 to always inline schemas
  servers:
    ...
```

See [Concepts: MCP](../../concepts/tools-integrations/mcp.md) for the protocol overview and How-to: use an MCP server for the end-to-end quickstart.

## `skills` block

Registers `SKILL.md`-based skills — the same explicit-registration model as `mcp.servers` (no directory scan; an entry must exist for a skill to be visible).

```yaml
skills:
  entries:
    pdf_editing:
      path: skills/pdf-editing/SKILL.md   # project-root-relative or absolute
      description: "Fill, merge, and extract fields from PDF forms"
      enabled: true
      auto_invoke: true
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `path` | string | required | Path to `SKILL.md`, or its containing directory. |
| `description` | string | `""` | One-line summary shown in the model-facing `## Skills` menu (first line only, 200-char cap). |
| `enabled` | bool | `true` | `false` removes the entry from the registry entirely. |
| `auto_invoke` | bool | `true` | `false` keeps the skill registered but excludes it from the system-prompt menu. |

`skills.entries` merges across `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml` ⊕ the dynamic `<project>/.reyn/config/skills.yaml` (written by the `skill_management__install_local` / `skill_management__install_source` chat tools), later tiers winning on name collision — the same merge shape as `mcp.servers`.

See [Concepts: Skills](../../concepts/tools-integrations/skills.md) for the full registration model, the three-layer exposure model (menu / on-demand read / bundled assets), and the install tools.

## `embedding` block

RAG embedding model classes and batch settings. Built-in defaults cover the OpenAI path — no `reyn.yaml` changes are required for a fresh install with `OPENAI_API_KEY`.

> **Non-OpenAI embeddings behind a LiteLLM proxy.** If your embedding
> class routes through a LiteLLM proxy to a non-OpenAI provider (e.g. an
> OpenAI-named route like `text-embedding-3-small` that the proxy maps to
> `gemini-embedding-001`), the proxy may add `encoding_format` — which Gemini
> rejects (`UnsupportedParamsError`), and the **action embedding index build
> fails → `search_actions` is disabled** (the retrieval scheme goes dead). The
> fix is **proxy-side**: set `litellm_settings:\n  drop_params: true` on your
> LiteLLM proxy so it drops provider-unsupported params. (The client-side flag
> does **not** apply on the proxy route — a known litellm behaviour. For a
> *direct* non-proxy embedding call, reyn already passes `drop_params=True`.)
> Alternatively use an OpenAI-compatible embedding class, or set
> `action_retrieval.embedding_class: null` to opt out. reyn surfaces this exact
> guidance when the index build fails with an `UnsupportedParamsError`.

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

| Class | Model | Notes |
|-------|-------|-------|
| `light` | `openai/text-embedding-3-small` | Needs `OPENAI_API_KEY`. |
| `standard` | `openai/text-embedding-3-small` | Needs `OPENAI_API_KEY`. |
| `strong` | `openai/text-embedding-3-large` | Needs `OPENAI_API_KEY`. |
| `local-mini` | `sentence-transformers/all-MiniLM-L6-v2` | Requires `pip install 'reyn[local-embed]'`; without the extras, instantiating raises at first `embed()` call (the `search_actions` visibility gate degrades to hidden gracefully). |
| `local-e5` | `sentence-transformers/intfloat/multilingual-e5-small` | Same `local-embed` extras requirement; multilingual model (better recall on non-English corpora). |

See [Concepts: RAG — local embedding backend](../../concepts/data-retrieval/rag.md#local-embedding-backend-fp-0043) for cache locations and trade-offs.

## `chat` block

Chat fills the context window with raw turns first; compaction fires when the
history exceeds the effective trigger (window-relative, derived from
`component_weights` against the model's actual context window). Head and tail
zones are **token-budgeted**, not turn-count gated.

```yaml
chat:
  compaction:
    # Budget allocation: integer weights, normalised at runtime.
    # Keys: head / body / tail / new_msg / compaction_batch
    component_weights:
      head:             10
      body:             5
      tail:             15
      new_msg:          10
      compaction_batch: 60
    section_caps_spec_tokens: 100
    use_chars4_estimate: false        # true = len(text)//4 (latency opt-out)
    body_token_cap: 1500               # hard cap on summary body tokens (post-truncation)
    resummarize_passes: 1              # LLM re-compression passes before hard_truncate floor
    # Section budget weights within body, normalised at runtime.
    section_weights:
      topic_arc:            5
      decisions:            40
      pending:              25
      session_user_facts:   10
      artifacts_referenced: 35
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
| `component_weights` | map[str,int] | `{head:10, body:5, tail:15, new_msg:10, compaction_batch:60}` | Integer weights for each prompt component, normalised to `main_pool` at runtime. Sum is arbitrary; larger values give more token budget to that component. |
| `section_weights` | map[str,int] | (per-section default) | Integer weights for sub-section allocation within the body budget. Same shape semantics as `component_weights`. |
| `section_caps_spec_tokens` | int | `100` | Static overhead budget for `section_token_caps` serialisation in the compactor prompt. |
| `body_token_cap` | int | `1500` | Hard cap on summary body tokens after post-truncation. |
| `resummarize_passes` | int | `1` | Max LLM re-compression passes when a produced `topic_arc` overshoots its body budget, before the deterministic `hard_truncate` floor. `0` = skip re-summary (straight to the floor). |
| `use_chars4_estimate` | bool | `false` | When `true`, use `len(text)//4` for token estimation instead of `litellm.token_counter` (latency opt-out for large deployments). |

### `chat.compaction.section_token_caps` fields

| Field | Default | Description |
|-------|---------|-------------|
| `topic_arc` | `200` | Token cap for the topic-arc summary section. |
| `decisions` | `400` | Token cap for the decisions section. |
| `pending` | `400` | Token cap for the pending-items section. |
| `session_user_facts` | `200` | Token cap for user-facts carried across compactions. |
| `artifacts_referenced` | `300` | Token cap for artifact reference listings. |

### Removed keys

`head_size`, `tail_size`, `trigger_total_tokens`, and `min_compact_batch` are
no longer recognised. If present in your `reyn.yaml`, Reyn emits a
`DeprecationWarning` at startup and ignores them. Remove these keys — head/tail
sizing is now token-budget via `component_weights`, and auto-compaction is
window-relative.

## `events` block

Audit-log rotation policy for chat-session event files. Skill-run events use one file per run and are not affected by this setting.

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

## `multimodal` block

Controls how Reyn handles binary media (images from `web__fetch` / `file__read` / MCP servers) and where multimodal artefacts live on disk.

```yaml
multimodal:
  max_bytes: 5000000              # 5 MB — Anthropic per-image API limit
  on_oversize: ask                # ask | allow | deny
  media_dir: .reyn/media          # project-relative dir for image binaries
  tool_results_dir: .reyn/tool-results   # project-relative dir for tool-result dumps
  base_url: null                  # optional canonical URL prefix for cross-host path_ref
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_bytes` | int | `5000000` (5 MB) | Decoded-payload byte cap before the on-oversize gate fires. Counts the binary size (`len(response.content)` / `len(file_bytes)`), not the base64-encoded shape. |
| `on_oversize` | string | `ask` | What to do when a piece of media exceeds `max_bytes`: `ask` (prompt the user via the intervention bus with size + source info; yes loads the media, no drops it), `allow` (silently accept; use in trusted non-interactive pipelines), `deny` (silently reject; the op returns `status="denied"` — use in cost-sensitive contexts). |
| `media_dir` | string | `.reyn/media` | Project-relative directory for image binary storage. Files are flat-named with timestamp + chain-id + tool prefix so `ls -la` sorts chronologically. Operator-browseable and operator-deleteable. |
| `tool_results_dir` | string | `.reyn/tool-results` | Project-relative directory for text-y tool result dumps. |
| `base_url` | string \| null | `null` | Optional canonical URL prefix for cross-host `path_ref` consumption. When set (e.g. `"https://reyn.example.com"` from a deployed `reyn web`), saved artefacts carry a `url` field pointing at `<base_url>/agents/<agent>/tool-results/<artifact>` so A2A peers / MCP clients / browsers can fetch the body via the resources router. Unset → no `url` field minted (same-host fast-path only). |

## `external_transports` block

Inbound transport → MCP tool routing for chat. Maps an external transport name (Slack / LINE / Discord / ...) to the MCP tool that delivers replies, plus an `args_template` describing how router output is shaped into the tool's arguments.

```yaml
external_transports:
  transports:
    slack:
      mcp_tool: slack__post_message
      args_template:
        channel: "${TRANSPORT_DEST}"
        text: "${ROUTER_REPLY}"
    line:
      mcp_tool: line__push_message
      args_template:
        to: "${TRANSPORT_DEST}"
        messages:
          - type: text
            text: "${ROUTER_REPLY}"
```

| Field | Type | Description |
|-------|------|-------------|
| `transports.<name>.mcp_tool` | string | Fully-qualified MCP tool name (`<server>__<tool>`) that delivers the reply. |
| `transports.<name>.args_template` | map | Shape passed to the MCP tool. `${TRANSPORT_DEST}` resolves to the per-message destination identifier (channel / user / room id), `${ROUTER_REPLY}` to the router's final text. Other `${VAR}` references resolve from `os.environ` per the standard interpolation rules. |

See `src/reyn/runtime/external_routing.py` for the per-transport contract and the full set of available template variables.

## See also

- `reference/config/permissions.md` — full permission grammar
- `reference/config/state-dir.md` — `.reyn/` layout
- [Concepts: MCP](../../concepts/tools-integrations/mcp.md)
- [Concepts: secret handling](../../concepts/runtime/secret-handling.md) — `~/.reyn/secrets.env` and `${VAR}` interpolation
- [Reference: `reyn secret`](../../reference/cli/secret.md) — managing secrets via CLI
- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — MCP server management CLI
