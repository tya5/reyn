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
llm:
  model: standard
  models:
    light:    gemini-flash-lite
    standard: openai/gpt-4o
    strong:   anthropic/claude-3-5-sonnet-20241022
```

## Top-level keys

**How to read the 3 write/reload columns** (#4206, split from a single
`Written on / reload` column that used to say 3 different things at once —
architect ruling, issuecomment-5379310759: adding a 4th value to THAT
column made "both" ambiguous between "2 files" and "2 layers", the actual
cause of #5086/#5088 misreading each other):

- **Declared in** — which LAYER(s) may hold a value for this key: `project`
  (`reyn.yaml`/`reyn.local.yaml`/`~/.reyn/config.yaml` only — the default,
  every row not called out otherwise), `project · agent` (an agent's own
  `.reyn/agents/<name>/profile.yaml` may also set it), `project · agent ·
  session` (a session's own `<session-state-dir>/config.yaml` may set it
  too), or `agent` alone (no project-wide value exists for this key at
  all). **No key is `agent` alone today** — the value names a real boundary
  case (a key with no project-wide value at all), not an occupied one.
  **This is the ONE
  column derived/checked in CI** (see below) — NOT because ② and ③ can't
  go stale (they can and do: ⁵ below is a real, measured instance for
  `Reload`), but because "does this call site re-read live?" is a
  property of THE CALL SITE, not something a file-based registry like
  `_HOT_RELOAD_FILES` can answer — there is no mechanical source to
  derive ②/③ FROM, so they stay hand-maintained and this doc's own
  correctness there rests on a human catching the next drift, same as
  before this split.
- **Reload** — `restart` (read once at `load_config` time) or `restart /
  hot` (the `.reyn/config/`-side write is re-read at the next turn
  boundary; the `reyn.yaml`-side write still needs a restart, same key).
- **File** — which file(s) on the **project** layer actually carry a value
  for this key: `reyn.yaml` alone, or `reyn.yaml` + a specific
  `.reyn/config/<name>.yaml` runtime registry. Agent/session-layer files
  are a DIFFERENT axis (see the note below the table) and are not
  enumerated here — this column is the project-layer write-gate boundary
  only (#2073: "the file split *is* the write-gate boundary").

The exception behind `restart / hot` is the runtime-mutable registries,
which may also be written in `.reyn/config/<name>.yaml`. **Only what is
written on that side is re-read at the turn boundary** (= hot reload); the
same key written in `reyn.yaml` waits for a restart like everything else.
The hot-reload loader structurally never opens `reyn.yaml` — to make a
setting hot-reloadable, put it under `.reyn/config/` rather than adding to
`reyn.yaml`.

**`_HOT_RELOAD_FILES` in `src/reyn/config/loader.py` is the source of which
keys get `restart / hot`.** The rows below are derived from it, but **do
not read a count or a name list out of this prose** — it does not follow
the registry when one is added.

**`Declared in`'s own source of truth**: `PREFERENCE_KEYS` in
`src/reyn/runtime/preferences.py` (the ③ preference axis, #4206) plus the
explicit agent-layer-only fields on `AgentProfile`
(`project_context_path`, #5086) —
`tests/repo/test_config_reference_declared_in_4206.py` checks this
table's `Declared in` cells against them in CI, mirroring
[`events.md`'s own doc↔code gate](../runtime/events.md). A key gaining
agent/session write capability without this table catching up → red.

⚠️ **One exception to the exception**: `composers` is read from the same 4-layer
combine as `hooks` (`reyn.yaml` ∪ `.reyn/config/hooks.yaml` ∪ per-agent ∪
per-session) but is **not** hot-reloaded — added or removed entries take effect
at the next session start.

Note that this table's axis is only the two **project-level** surfaces
(`reyn.yaml` and `.reyn/config/`). `hooks`, `composers`, and the permission keys
can additionally be written on an **agent** surface (`.reyn/agents/<name>/`,
`.reyn/capability_profiles/<name>.yaml`) and a **session** surface
(`<session-state-dir>/config.yaml`) — see
[permission-model](../../concepts/runtime/permission-model.md).

**Placement principle (#4174 T7)**: a setting nests under an owning block when
only that subsystem's own code path ever reads it (e.g. `embedding.cost_warn_threshold`
— read once, inside the embedding/indexing pipeline, nowhere else). It gets a
top-level block of its own when it's read by code outside any single subsystem
— reachable from more than one place a session can be, at more than one moment
(e.g. `cost_warn` — read by the chat router at any `/model` switch and at
session startup, unrelated to which subsystem, if any, is running). **A shared
substring in two keys' names is not evidence they're the same setting** — check
who reads the value and from how many call sites, not what it's called. See the
[`cost_warn` block](#cost_warn-block) for the specific pair that motivated writing
this down: `cost_warn` and `embedding.cost_warn_threshold` sound related and
aren't.

<!-- BEGIN config-declared-in -->
| Key | Type | Declared in | Reload | File | Description |
|-----|------|-----|-----|-----|-------------|
| `output_language` | string | project · agent · session¹ | restart⁵ | `reyn.yaml` | Default output language code (e.g. `en`, `ja`). Override with `--output-language`. |
| `safety` | map | project | restart | `reyn.yaml` | Runtime bounds **and content-layer defenses**: loop-detection caps, timeouts, on-limit policy, the untrusted-content threat scan + fence (`safety.threat_scan`, FP-0050), and operator bounds on the LLM spawn tree (`safety.spawn`, a DoS guard). See below. |
| `cost` | map | project · agent · session¹ | restart⁵ | `reyn.yaml` | Budget caps and rate limits (per-agent, daily, monthly). See below. |
| `web_fetch` | map | project | restart | `reyn.yaml` | The `web_fetch` tool + MCP registry calls: SSL settings. See below. |
| `gateway` | map | project | restart | `reyn.yaml` | The `reyn web` gateway's own settings: auth model, WebSocket inbound-frame ceiling, and which surfaces are mounted. Split from the old `web:` key, which conflated this with `web_fetch` above. See below. |
| `sandbox` | map | project | restart | `reyn.yaml` | Backend selection (`backend`), unsupported-platform policy (`on_unsupported`), the enforcement mode (`mode`: compat / strict / custom), and the agent-level sandbox policy (`policy`). See below. |
| `hooks` | list | project · agent · session² | restart / hot | `reyn.yaml` + `.reyn/config/hooks.yaml`² | Agent-lifecycle hooks. Four action schemes: `template_push` / `exec` / `exec_capture` / `pipeline_launch`. See below. |
| `embedding` | map | project | restart | `reyn.yaml` | RAG embedding: the master switch (`enabled`), model classes, batch sizing and concurrency, retry / backoff / timeout, tokenizer, and a cost-warning threshold. See below. |
| `chat` | map | project · agent · session¹ | restart⁵ | `reyn.yaml` | Chat-session runtime knobs: history compaction, reasoning/"thinking" text handling, the interactive renderer (`render_mode`), TUI gutters, body neutralization, permitted image-URL schemes, and the TUI theme name. See below. **Only `chat.reasoning.display` carries the ¹ override** (a single leaf, not the whole block — see [`chat.reasoning` fields](#chatreasoning-fields)); every other `chat.*` key stays `project`-only. |
| `voice` | map | project | restart | `reyn.yaml` | Whisper model/language/device settings for F2 dictation in the inline CUI (revived #4187/#4249). See below. |
| `audit_events` | map | project | restart | `reyn.yaml` | Rotation policy (size / age / cleanup period) for the P6 **audit-event** files under `.reyn/events`. Not WAL-events, not hook-events. See below. |
| `artifacts` | map | project | restart | `reyn.yaml` | The artifact-ref table fallback's own row cap (`remote_fallback_limit`, #4601) — used by a remote client's (and a post-restart local client's) Artifacts pane. See below. |
| `observability` | map | project | restart | `reyn.yaml` | Opt-in OpenTelemetry (OTLP) export of P6 audit-events. Off by default. See below. |
| `tool_use` | map | project | restart | `reyn.yaml` | Chat-layer tool-use scheme x transport selector (`scheme`, `transport`). See below. |
| `mcp` | map | project | restart / hot | `reyn.yaml` + `.reyn/config/mcp.yaml` | MCP server definitions. See below. |
| `agent_id` | string | project | restart | `reyn.yaml` | Agent **identity** — stamped on the P6 audit trail and the outgoing HTTP header. Does **not** define or configure agents; agent definitions live in `.reyn/agents/<name>/`. See below. |
| `auth` | map | project | restart | `reyn.yaml` | OAuth provider configurations for `reyn auth login`. See below. |
| `cron` | map | project | restart / hot | `reyn.yaml` + `.reyn/config/cron.yaml` | Scheduled skill executions. See below. |
| `external_transports` | map | project | restart | `reyn.yaml` | Inbound transport → MCP tool routing for chat (Slack / LINE / Discord etc.). See below. |
| `multimodal` | map | project | restart | `reyn.yaml` | Binary media (image/audio) size cap, on-oversize behaviour, artefact storage paths, and the `base_url` those artefacts are served under. See below. |
| `permissions` | map | project · agent · session² | restart⁷ | `reyn.yaml` | Default permission policy. See below. |
| `project_context_path` | string | project · agent³ | restart⁶ | `reyn.yaml` | Markdown file injected into every phase system prompt. Unset (default): auto-resolves the cross-tool standard — `AGENTS.md` if present, else `REYN.md` (legacy fallback). Set an explicit path to pin one file; set `""` to disable. **#5084: an agent's own `.reyn/agents/<name>/profile.yaml` may set `project_context_path` too, REPLACING (not merging with) this project-wide value for that one agent** — a separate file/mechanism from this `reyn.yaml` key. See note below. |
| `llm` | map | project | restart | `reyn.yaml` | LLM-layer config: model selection (`llm.model` default class, `llm.models` class → LiteLLM string map, `llm.model_class_by_purpose` per-purpose override, `llm.api_base` proxy URL, `llm.prompt_cache_enabled`), plus routing (#1829) and retry (#1835). See below. |
| `delegation` | map | project | restart | `reyn.yaml` | Cross-agent delegation policy (#2081). |
| `cost_warn` | map | project | restart | `reyn.yaml` | High-cost-model gate (#1830 / FP-0052): warns before an expensive model is selected — and, despite the name, **can block it** (`cost_warn.block_on_high_cost`). See below. |
| `offload` | map | project | restart | `reyn.yaml` | Opt-in switch for the tool-result size gates. |
| `render_template` | map | project | restart | `reyn.yaml` | Operator-tunable output bounds for the `render_template` op (FP-0055 / #2679). |
| `fs_watch` | map | project | restart | `reyn.yaml` | Operator-declared filesystem watch paths (#2608 H4). |
| `composers` | list | project · agent · session² | restart⁴ | `reyn.yaml`⁴ | Composer definitions. Empty (default) → `start_composers` is never called. |
| `skills` | map | project | restart / hot | `reyn.yaml` + `.reyn/config/skills.yaml` | Skill declarations. Merged across config tiers by name (an explicit entry wins a collision). |
| `pipelines` | map | project | restart / hot | `reyn.yaml` + `.reyn/config/pipelines.yaml` | Pipeline declarations. Same union-merge shape as `skills`. |
| `presentations` | map | project | restart / hot | `reyn.yaml` + `.reyn/config/presentations.yaml` | Presentation-template declarations. Same union-merge shape as `skills` / `pipelines`. |
| `tui` | map | project | restart | `reyn.yaml` | Operator-tunable inline-TUI presentation thresholds — today just the status bar's context-usage-percent warn threshold. See below. |
<!-- END config-declared-in -->

¹ **`Declared in` beyond `project`, checked in CI** — the ③ preference axis
(#4206): `output_language`, `chat.reasoning.display`, and the 7
`cost.*.warn_ratio` leaves are `PREFERENCE_KEYS` (`src/reyn/runtime/
preferences.py`) — freely overridable in an agent's own `profile.yaml`
**and** a session's own `config.yaml`. `cost`'s OWN row stays `project ·
agent · session` at the block level (rather than "project" + a footnote
like `chat`) because the `warn_ratio` leaves span all 6 of `cost`'s
budget-cap sub-blocks, not one single nested field.

² `hooks`/`composers`/`permissions` are already disclosed above the table
as agent+session-writable via their own layered COMBINE (a DIFFERENT
mechanism from the ③ preference axis ¹ uses — see [permission-model
](../../concepts/runtime/permission-model.md)) — not new information, just
reflected into this column now that it exists on its own. `hooks`'s own
`File` column stays project-layer-only, same scoping as every other row
(agent/session hook files live under `.reyn/agents/<name>/hooks.yaml` /
`<session-state-dir>/hooks.yaml` — a 3rd/4th file this column does not
enumerate, per the "project-layer write-gate boundary only" rule above).

³ #5086. No session-layer override exists for this key (unlike ¹'s
`PREFERENCE_KEYS` set) — an agent's own `profile.yaml` REPLACES the
project-wide file for that one agent; there is nothing for a session
layer to further compose with.

⁴ **Unconfirmed during this pass** — the pre-split text asserted a
`.reyn/config/`-side write surface for `composers` ("both (but not
hot-reloaded)"), but `_HOT_RELOAD_FILES` (`src/reyn/config/loader.py`)
does not list a `composers.yaml`, and `Session._build_composer_defs`'s own
"runtime" layer reads `in_set.get("composers")` — which `load_hot_reload_
config` (the ONLY producer of that `in_set`) has no path to populate. Kept
conservative (`reyn.yaml` only) rather than repeating a claim this pass
could not verify; flagged rather than silently resolved either way — see
this table's own PR for the open question.

⁵ **`Reload` is `restart` at the PROJECT layer only** (lead-coder's own
#5090 review finding — issuecomment-5379469534) — the SAME ¹ rows
(`output_language`, `chat.reasoning.display`, the 7 `cost.*.warn_ratio`
leaves) are LIVE re-reads at the agent/session layer (`Session.
output_language`/`_resolve_session_preference`, verbatim "Live re-read on
every access"/"Live re-read on every call (never cached)") — a DIFFERENT
mechanism from `_HOT_RELOAD_FILES`'s file-based hot-reload this column's
own intro describes, and structurally invisible to it (a property-access
re-read, not a file re-read). This footnote points at the SAME
`PREFERENCE_KEYS` set as ¹, so a future ① gate failure on one of these
rows carries this footnote's claim along with it, rather than the two
drifting independently.

⁶ **`Reload` is also not a bare `restart` at the agent layer** — a
DIFFERENT mechanism from both ¹ (a live property-access re-read on every
call) and `_HOT_RELOAD_FILES` (a file re-read on every turn boundary):
`project_context_path`'s agent-layer override is resolved ONCE PER AGENT,
at session construction (`registry_bootstrap.resolve_agent_project_
context`, called from `chat.py`'s own `_session_factory` closure via
`AgentRegistry`). A NEW
session picks up whatever `profile.yaml` currently says with no
project-layer restart needed; an ALREADY-RUNNING session does not notice
a `profile.yaml` edit until its own next construction (the next
`--connect`/re-attach), which is neither "restart" (project-layer,
process-wide) nor "hot" (file-based, mid-session) in this column's other
two senses.

⁷ **`Reload` is also not a bare `restart` at the agent/session layer** —
`permissions`'s agent/session-layer narrowing (`tool_allow`/`tool_deny`/
`mcp_allow`/`mcp_deny` etc., composed via `capability_visibility.py`) is
LIVE-reapplied through `reapply_visibility_override` (#2285, verbatim
"the change is live next turn") whenever the per-session config is
re-read at a hot-reload boundary — closer to ¹'s live-reread family in
practical effect, though via a distinct capability-composition mechanism,
not `PREFERENCE_KEYS`.

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

## Per-agent profile key reload classes (#4206 slice 1)

Every key an agent's own `.reyn/agents/<name>/profile.yaml` can set — the
surface an operator actually touches when hand-authoring an agent — has a
declared **reload class**: how soon an edit to that key takes effect.
Source of truth: `AGENT_PROFILE_RELOAD_CLASSES` in
`src/reyn/runtime/profile_reload.py` — this table is a projection of that
dict, not a second hand-written source; a key added there with no
matching row here (or vice versa) fails
`tests/runtime/test_4206_slice1_profile_reload_class.py`'s completeness
gate.

**Scope, explicit**: only per-agent-profile keys. Every OTHER key in the
[table above](#top-level-keys) stays as documented there — this slice does
not attempt project-layer or session-layer reload classes.

- **`live`** — re-read on every access, no caching. A caller holding the
  value across an await must re-read it, never cache it.
- **`construction-once`** — resolved exactly once per agent, at session
  construction. An already-running session does not notice a
  `profile.yaml` edit until its own NEXT construction (next
  `--connect`/reattach).
- **`explicit-trigger`** — a bare hand-edit of `profile.yaml` does NOTHING
  on its own; reapply only happens when something else calls
  `request_reload` (`/reload`, or an LLM hooks-write op), applied at the
  next turn boundary. **Not** the same as the [table above](#top-level-keys)'s
  own `restart / hot` — that class's defining property is the file WRITE
  ITSELF being the trigger (`.reyn/config/`-side writes are picked up
  automatically at the next turn boundary); `profile.yaml` is not in
  `_HOT_RELOAD_FILES`, so a hand-edit alone changes nothing until an
  explicit trigger fires — the opposite polarity from `hot`, hence the
  different name (measured during this slice: `allowed_mcp` fits none of
  the other 3 classes cleanly).
- **`restart`** — takes effect only after a full process restart. No
  slice-1 key currently uses this value.

<!-- BEGIN agent-profile-reload-class -->
| Key | Reload class |
|-----|---------------|
| `role` | `construction-once` |
| `allowed_mcp` | `explicit-trigger` |
| `base_dir` | `live` |
| `project_context_path` | `construction-once` |
| `sandbox` | `live` |
| `preferences.output_language` | `live` |
| `preferences.chat.reasoning.display` | `live` |
| `preferences.cost.per_agent_tokens.warn_ratio` | `live` |
| `preferences.cost.per_agent_cost_usd.warn_ratio` | `live` |
| `preferences.cost.daily_tokens.warn_ratio` | `live` |
| `preferences.cost.daily_cost_usd.warn_ratio` | `live` |
| `preferences.cost.monthly_tokens.warn_ratio` | `live` |
| `preferences.cost.monthly_cost_usd.warn_ratio` | `live` |
| `preferences.cost.rate_limit_warn_ratio` | `live` |
| `bounding.model` | `live` |
<!-- END agent-profile-reload-class -->

## `llm` block

LLM-layer config: model selection (`llm.model` / `llm.models` /
`llm.model_class_by_purpose` — this section — plus `llm.api_base` /
`llm.prompt_cache_enabled` below), and `llm.router` / `llm.retry` (further
below: opt-in litellm.Router + Reyn self-retry backoff timing). #4174 T3:
model selection moved here from top-level `model:` / `models:` /
`model_class_by_purpose:` keys of the same name — same shapes, only the
nesting changed.

### `llm.models` block

Each entry under `llm.models:` maps a class name to a LiteLLM model string **or** a dict that declares per-class LLM parameters.

### Model classes vs model names — the resolution rule

Two kinds of position appear in config, and they follow opposite rules. The same rule applies to the completion `models:` block **and** the `embedding.classes:` block.

- **Class position** (a *reference* to a class): `model`, per-agent / per-phase / per-op model overrides, `embedding.default_class`. These are **closed-world** — the value must name a class that exists in `models:` / `embedding.classes:` (or a built-in tier: `light` / `standard` / `strong`). A value that is not a known class is **not** silently treated as a literal model:
  - operator config (`model:` in reyn.yaml) keeps a backward-compatible literal passthrough (you may put `openai/gpt-4o` directly);
  - a **skill/op-supplied** model (`op.model`) that is not a known class is **rejected** and falls back to the runtime model (one warning), so a skill- or LLM-authored string cannot bypass the proxy config — the proxy config is the single source of truth for model selection.
- **Name position** (the *definition* of a model): the `model:` value inside a `models:` / `embedding.classes:` entry. A name should be `provider/model` (e.g. `openai/gpt-4o`, `openai/nomic-embed-text` for a local model served behind a litellm proxy). A bare name with no `/` is accepted (some LiteLLM strings are bare) but **warns** at load — add the prefix if resolution misroutes.

In one line: **a `_class` / tier position takes a class name (closed-world); a `model` position takes `provider/model` (validated). No position accepts both.**

### str form — literal (backward compatible)

If a str value **contains `/`**, it is treated as a literal LiteLLM model string:

```yaml
llm:
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
llm:
  models:
    standard: claude-sonnet-thinking     # equivalent to: standard: {extends: claude-sonnet-thinking}
```

An unknown shorthand (name not in user entries or built-ins) is a startup error.

### dict form — plain kwargs

```yaml
llm:
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
| `api_base` | no | Per-class endpoint override — a routing field, not forwarded as a litellm kwarg. |
| `provider` | no | Per-class litellm `custom_llm_provider` — a routing field, not forwarded as a litellm kwarg. |
| `stream` | no | Whether calls for this class stream. A **reyn** field, not forwarded to litellm; overrides the capability query in both directions. Omit to let reyn decide — see below. |
| `stream_options` | no | **Not settable.** No reyn meaning, and it breaks the collect-whole branch — **rejected at load**. |
| `max_input_tokens` | no | Operator-declared context-window CEILING for this class (#4689). A **reyn** field, not forwarded to litellm — takes priority over the LiteLLM catalog UNCONDITIONALLY (not just when the catalog lookup fails). Omit to let reyn read the catalog, falling back to a conservative 128,000-token default if the model isn't cataloged. |
| *(any other field)* | no | Silently passed through to litellm (passthrough policy). |

> **Cost limit**: use `max_completion_tokens`, not `max_tokens`.  `max_tokens` is a legacy
> soft hint that many providers ignore; it has no enforcement power on OpenAI o1+ or
> Anthropic models.  `max_completion_tokens` is enforced at the API level.

**Field policy**: `model` is the only required field. Most other fields are passed directly to `litellm.acompletion` without validation — unknown fields are silently forwarded (future-proof); typos cause silent litellm failures, not reyn errors. Four exceptions are handled at load instead: `reasoning_effort` (below), `stream` (consumed by reyn, type-checked), `stream_options` (**rejected outright**), and `max_input_tokens` (consumed by reyn, type-checked — never forwarded to litellm, closing the same "accepted but silently does nothing" gap the other three exceptions close).

### `stream` — whether this class's calls stream

Reyn decides this per call, from a litellm capability query inside the single
completion funnel (`recorded_acompletion`). `stream:` is the operator's answer,
and it **wins over the query in both directions**:

```yaml
llm:
  models:
    my-new-model:
      model: some-model-too-new-for-litellm
      stream: true      # stream, whatever the catalog says
    picky-endpoint:
      model: openai/gpt-5
      stream: false     # never stream, even though the catalog allows it
```

Omit the field to leave the decision to reyn. That is the right default; set it
when you know something the catalog does not.

You often will. Reyn pins litellm's model table to the snapshot bundled with the
installed package (`LITELLM_LOCAL_MODEL_COST_MAP`, set in `reyn/__init__.py` to
silence a startup network fetch), so a model newer than that snapshot is absent
from it — permanently, not intermittently. An absent row is not a statement that
the model cannot stream, and reyn no longer reads it as one; but where the
catalog is actively wrong, this field is how you say so.

This is a **reyn** field: it is consumed by the streaming decision and never
forwarded to `litellm.acompletion`. That distinction is load-bearing — as an
ordinary passthrough kwarg it reached the collect-whole branch and made litellm
return a stream object that reyn read as a finished reply, surfacing as an error
naming neither `stream` nor the config:

```
EmptyLLMResponseError: LLM returned a 200 response with empty choices
(model=...); provider response: <litellm...CustomStreamWrapper object...>
```

A non-boolean value is rejected at load. Unlike the forwarded fields, a typo
here would otherwise never reach anything that could complain about it.

### `stream_options` (not settable)

Rejected at config load (`ValueError`, fail-fast). It has no reyn meaning, and
riding the kwargs passthrough it produces the same broken shape described
above.

### `max_input_tokens` (per-class context-window ceiling, #4689)

Declare the context window reyn should budget against for a class — overriding
the LiteLLM catalog **unconditionally**, not just when the catalog lookup
fails:

```yaml
llm:
  models:
    gpt-5-6-luna:
      model: openai/gpt-5.6-luna
      max_input_tokens: 128000   # operator's own answer, wins over the catalog
```

Every consumer of `reyn.llm.model_budget.get_max_input_tokens` (compaction, the
turn-budget force-close threshold, the status-bar context chip, ...) only ever
holds an already-resolved LiteLLM model STRING, never a class name — none of
those call sites change. Instead, at each `reyn.llm.model_resolver.ModelResolver`
construction (session/CLI/web startup), every class's declared
`max_input_tokens` is resolved to its model string and registered into a
process-shared table `get_max_input_tokens` consults FIRST, ahead of the
catalog.

**Two classes resolving to the SAME model string must agree.** If `light` and
`standard` both point at `openai/gpt-4o` but declare different
`max_input_tokens` values, registration raises
`reyn.llm.model_budget.MaxInputTokensConflictError` — which value should win is
ambiguous, so reyn refuses to guess (give them the same value, or point them at
different model strings). The SAME check applies across sessions sharing one
process with different configs — a later, conflicting declaration for a model
string another session already registered raises rather than silently
overwriting the first.

A non-positive or non-integer value is rejected at load, the same discipline
`stream` above uses — a config typo here would otherwise silently ride
`spec.kwargs` into `litellm.acompletion` as an unrecognized kwarg (accepted,
does nothing).

Complementary, not exclusive: some LiteLLM proxies also advertise a model's
window via `GET /model/info` (verified live for some providers, not yet for
others) — `max_input_tokens` here is the operator's own, explicit override,
useful specifically when that proxy-side declaration is absent, wrong, or the
model predates reyn's bundled catalog snapshot.

### `reasoning_effort` (per-model reasoning budget)

Set how much the model is allowed to "think" before answering. Declared per model
definition so it's explicit and easy to understand:

```yaml
llm:
  models:
    light:
      model: gemini/gemini-2.5-flash-lite
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
  llm:
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
> reasoning text, displays it (TUI + web, collapsible — `chat.reasoning.display`),
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
llm:
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

**Unknown references**: referencing a name not in the namespace is a startup error —
including `light` / `standard` / `strong` themselves if your `reyn.yaml` doesn't map
them (see below).

### No built-in model catalog

Reyn ships **no** built-in catalog of concrete provider/model targets — `light`,
`standard`, and `strong` are reyn's own vocabulary (the 3 standard tiers, in
ascending cost order), but what each one actually points to is entirely up to your
`llm.models:` mapping above. `reyn init` writes a starting mapping into the
`reyn.yaml` it generates; edit it to match your provider. A class with no mapping
in effect (no `reyn.yaml`, no `reyn.local.yaml`, or a `models:` block that omits
it) is a startup error naming the missing class — reyn does not silently fall back
to a hidden default for any class, tier or custom.

### `llm.model_class_by_purpose` — per-purpose model class

Reyn makes several internal LLM calls beyond the main agent reply, each tied to a
logical **purpose**. By default every purpose uses your configured `model` (the
default class) — **routing follows the model you configured; there is no hidden
cheaper tier**. `model_class_by_purpose` lets you override the class for a
specific purpose; an unset purpose falls back to `model`.

| Purpose | What it covers |
|---|---|
| `router` | The per-turn chat router / intent classification. |
| `tool` | The default class for tool-spawned skill runs. |
| `judge` | Output-judging / evaluation calls. |

```yaml
llm:
  model: standard                  # the default class for every purpose
  models:
    standard: openai/gpt-5.4
    light:    openai/gpt-4o-mini
  model_class_by_purpose:
    router: light                  # opt INTO a cheaper per-turn router (an explicit choice)
    # tool / judge unset → follow `model` (gpt-5.4)
```

**Cost note**: the router runs on every turn, so the cheap-router optimisation is
still available — it is now an explicit one-line opt-in (`router: light`) rather
than a hidden default. Explicit per-call selections (a skill's `op.model`) still
win over this fallback. Unknown purpose keys are warned (not fatal) at load time —
**except `compaction`** (#3785), which fails to load: compaction used to be
configurable here but never tracked a `/model` switch mid-session, so it always
follows the conversation's active model now, and a config that still sets this
key is refused with a remedy rather than silently ignored.

### `llm.model_max_class` — model-class ceiling (#4206 T1)

`model_max_class` declares an operator ceiling on the model class any call may
use (`light` / `standard` / `strong`, the same cost order every tier list
uses). It is **restrict-only, reject-not-clamp** — the same shape as
`sandbox.max_timeout_seconds`'s LLM-extensible ceiling: a call whose resolved
class exceeds the ceiling is REJECTED, naming the actual ceiling, before
`litellm.acompletion` is ever invoked — never silently downgraded to a
cheaper class.

```yaml
llm:
  model: standard
  model_max_class: standard    # a call resolving to "strong" is rejected
```

Unset (default) means unbounded — byte-identical to every deployment before
this field existed. Enforcement happens once, inside `recorded_acompletion`
(the single #1190 cost-observability chokepoint every LLM call passes
through) — **a call site that resolved a model CLASS cannot forget to apply
it**, not "a new call site cannot forget to apply it" — the ceiling
compares against a class, and #4324 records two structural (not
implementation) exceptions where no class ever reaches this chokepoint:

- **A raw model string** — `resolve_purpose_class` returns it unchanged;
  there is no class to compare against, so the ceiling is a no-op for that
  call. Not a bug: a raw string is, by construction, outside the
  `light`/`standard`/`strong` vocabulary this field's ceiling is defined
  over.
- **A `model_class=None` call** — `dev/dogfood/interpretation.py` and
  `dev/dogfood/verifiers/reply.py` both call out with no class at all
  (real-cost calls, not free/auxiliary ones), so they never enter the
  comparison this field performs.

`model_max_class` is a **declared** boundary — complete only within its own
vocabulary (a call that resolves to a class). It is not the tool for
covering every call regardless of vocabulary; a **measured** boundary
(`cost.*` below) is. Owner ruling, 2026-08-15: **cost caps stay opt-in** —
reyn does not turn one on by default, matching the standing "security/cost
gates are opt-in, UX and predictability come first" policy. Concretely:
**in an environment with no `cost.*` hard limit configured, a raw
model-string call has no ceiling at all** — neither this field's class
comparison (structurally can't apply) nor a cost cap (unset by default)
bounds it. An operator who wants that call bounded sets a `cost.*` hard
limit explicitly (see the `cost` block below); nothing bounds it for them
otherwise.

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
| `retry_policy` | map\|null | `null` | Per-exception-type retry counts. Absent (null) → litellm defaults (`num_retries` applies uniformly). When set, constructs a `litellm.RetryPolicy` and passes it to the Router. Supported keys: `RateLimitErrorRetries`, `TimeoutErrorRetries`, `BadRequestErrorRetries`, `AuthenticationErrorRetries`, `ContentPolicyViolationErrorRetries`, `InternalServerErrorRetries`. |

On the Router path, retry count is **config-only**: `num_retries` is taken from
`llm.router.num_retries` (a per-call `max_retries` is not applied), so the retry
budget has a single source. (On the direct, non-Router path the per-call
`max_retries` is unchanged.)

**What degrades if litellm's Router misbehaves** (#4354 follow-up — owner's
"delegate what litellm already does" ruling means reyn increasingly relies on
litellm's own retry/fallback/cooldown machinery working correctly, not a
reason by itself to stop delegating): `num_retries` / `fallbacks` /
`cooldown_time` / `allowed_fails` / `retry_policy` above are all config reyn
merely HANDS to `litellm.Router` — reyn keeps no parallel bookkeeping of
deployment health, cooldown state, or which fallback fired. If the Router's
own cooldown/fallback logic misbehaves (wrong deployment skipped, a cooldown
that never clears, a fallback chain that doesn't fire), reyn has no
independent view to detect or override it — debugging that is inspecting
litellm's own Router state/logs, not reyn's, since (post-#4347/#4354) reyn no
longer holds a second copy of per-deployment credential/rotation state to
cross-check against. This is the same shape #4398's `chars//4` token-count
fallback names for `token_counter`'s own failure mode: delegating is the
right call, but the doc should say what the delegation costs when the
delegated-to system is wrong, not just that delegating is correct.

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
model reasoning/"thinking" text handling. `chat.render_mode` selects the
interactive chat renderer/driver. `chat.gutters` sets the TUI conversation
pane's two gutter columns' start state. `chat.theme` overrides the interactive
TUI's Textual theme name.

```yaml
chat:
  render_mode: alt-screen # interactive chat renderer: alt-screen (default) | plain
  reasoning:
    continuity: true      # persist reasoning to history + replay recent turns
    display: true         # show reasoning in the UI (TUI + web, collapsible)
    recent_turns: 3       # turns of reasoning to replay; <=0 = unbounded
  gutters:
    left: true            # TUI left gutter (state marker, 2 cols) shown at start
    right: true           # TUI right gutter (elapsed/tokens, 12 cols) shown at start
  neutralize_body: false  # opt-in ESC/OSC strip on agent-reply/tool-result body text
  image_url_schemes: []   # opt-in scheme allowlist for present's image src fetch
  empty_stop_retry: false # opt-in resend on an empty router response
  theme: null              # override the TUI's Textual theme name (default: reyn's own)
  stream_repaint_min_interval: 0.0333  # seconds between repaints of a streamed reply
```

### `chat.render_mode`

Selects which renderer/driver `reyn chat` uses on a **TTY**. A **non-TTY**
session (piped, CI, or a host with no real terminal) always falls back to
`plain` regardless of this value — the interactive Textual drivers need a real
terminal.

| Value | Behaviour |
|-------|-----------|
| `alt-screen` | **Default.** Full-screen Textual (alt-screen driver). Terminal scrollback is auto-saved on enter and restored on exit; the previous conversation is rebuilt from `history.jsonl` on restart. This is the recommended mode. |
| `plain` | Force the plain line-based renderer (`ConsoleChatRenderer`, no Textual), genuinely equivalent to `--cui` — same renderer and same input-loop driver, not just the input driver. |

An unrecognised value — including a stale `inline` or `auto` from before this
table was narrowed to two values (owner instruction, `inline`'s legacy
bounded driver was removed; `auto` was behaviourally identical to
`alt-screen` and carried no distinct behaviour) — warns and falls back to
`alt-screen`.

### `chat.gutters` fields

The TTY conversation pane draws two fixed-width gutter columns — a left state
marker (2 columns) and a right elapsed/token readout (12 columns) — and each
costs its width on **every** row. These two flags set what the pane opens
with; they are independent, matching the underlying widget's own granularity.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `left` | bool | `true` | Show the left (state-marker) gutter when the pane opens. |
| `right` | bool | `true` | Show the right (elapsed / turn-token) gutter when the pane opens. |

Either can also be toggled at any time from the keyboard — `ctrl+g` (left) and
`ctrl+t` (right), both listed in the TUI's Help pane. **A keyboard toggle is
session-scoped**: it changes the running pane only and never writes back here,
so these keys are the setting to change for a lasting preference. Hiding a
gutter hands its whole column back to the conversation body (an 80-column
terminal's body goes 66 → 78 columns with the right gutter hidden, → 80 with
both).

### `chat.neutralize_body`

Opt-in ESC/OSC-control-sequence strip on the agent-reply / tool-result **body**
text (#3318). Off by default — reyn's stated policy is "UX/predictability over
security, security is opt-in". LLM-derived choice labels and intervention
prompts are **always** neutralized regardless of this flag (#3302, a separate,
unconditional display-boundary fix); this flag widens the same terminal
neutralizer (`core/present/guard.get_neutralizer("terminal")`, ESC/control
strip) to the conversation body content itself, where a raw ESC/OSC sequence
from tool output or an untrusted model reply could otherwise reach the
terminal on both the TUI conversation pane and the plain (`--cui`) renderer.

Three more cases are always neutralized regardless of this flag, same shape
as #3302's, all structurally unable to read it (a different code path
reaches the same terminal neutralizer, not `_body_renderable` — the ONE
function this flag actually gates):

- A tool's own **one-line summary** (`summarize_tool_result` — the
  collapsed line shown above/instead of the full body, e.g. a failed
  `exec`'s `exit 1: <stderr detail>`) — unconditional since #4760.
  World-derived text (a sandboxed process's arbitrary stderr bytes)
  reaches that summary through every caller of `summarize_tool_result`.
- The inline TUI's **expanded tool-detail view** (Space-toggled, #4697 —
  `_result_detail_lines`/`_dict_detail_lines` in `textual_chat/presenter.py`)
  — unconditional since **#4757**, at its own seam: it takes no
  `neutralize_body` parameter at all, and never did. (Before #4757 this
  path was not neutralized — `json.dumps` merely escaped control bytes as
  a side effect, and the bare-string branch did not even do that.)
- A **failed tool call's own error line** (`tool_call_failed`'s
  `err`/`error_message`, shown on both the plain `--cui` renderer's
  `format_inline_message` and the TUI's `_tool_result_line`/
  `_body_and_background`) — unconditional since **#4770**. Traces to
  `dispatcher.py`'s own catch-all (`f"{type(e).__name__}: {e}"` wrapping
  any tool-handler exception — an MCP call, a sandboxed subprocess, a
  provider HTTP error), the same class #4760 fixed for
  `summarize_tool_result`'s stderr branch but explicitly did not cover —
  this is a structurally separate code path that never goes through
  `summarize_tool_result`'s own return boundary.

So `neutralize_body` widens the SAME terminal neutralizer to exactly one
more surface — the full, collapsed body text `_body_renderable` renders —
not to "tool-result rendering" as a whole; the summary line, the expanded
detail view, and a failed call's own error line are unconditionally
covered either way.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `neutralize_body` | bool | `false` | Strip ESC/control sequences from agent-reply and tool-result body text before rendering. |

### `chat.image_url_schemes`

Opt-in narrowing of which URL schemes `present`'s `image` component will
fetch (#3846, owner ruling C). Default `[]` — unrestricted, both `http` and
`https` are fetched — the owner's stated rationale: "even without the bytes,
the record of what was presented is enough" (the value is the audit record
that an image was presented, not reyn proxying/verifying the bytes). Set to a
non-empty list to restrict to exactly those schemes, e.g. `["https"]` to
reject plain `http`. Any scheme outside `{http, https}` is always rejected
regardless of this setting — nothing else is fetchable through an `httpx`
client. `src` is written by the model (`present`'s blueprint is LLM-authored),
so the fetch always routes through the SSRF-pinned client
(`_network.py`'s `build_async_http_client(pin_ssrf=True)`) unconditionally,
independent of this scheme setting.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `image_url_schemes` | list[str] | `[]` | Restrict `present`'s image-src fetch to these schemes; empty = unrestricted (http + https). |

### `chat.empty_stop_retry`

Opt-in retry of an empty router response (#4677). When the LLM returns
`finish_reason="stop"` with no content and no tool calls — a provider-level
glitch, observed at ~50% rate with weak models (B7-G12 measurement) — reyn
can resend ONE continuation nudge before surfacing the failure. **#5273/#5274:**
the nudge is no longer a bare `"resume"` — a bare word at the message's content
position read as a human instruction to some models, which replied with a
status report instead of continuing (observed live). It is now a self-describing,
attributed string (`"(reyn-auto-continue) resume - automatic continuation
signal from reyn's own scheduler, not an instruction from anyone. No reply
needed; continue the interrupted work."`) injected as a synthesized
`role="system"` message, not `role="user"`.
Owner default is now `false` (2026-08-14) — the retry was previously
hardcoded on in production with no config knob at all, until an incident
where 30 empty-response detections in one `reyn-self` run each cost a
second LLM call (30 turns → 63 `llm_called`). A measured benefit exists for
some environments (Trace-patch-replay: 0/10 → 10/10 narration recovery on a
specific empty-stop case with the retry on) — reports of this shape have
come from weaker/local-model setups (Qwen, LM Studio, Ollama, vLLM,
LiteLLM-fronted proxies); an operator on one of those sets this back to
`true`. This does **not** fix empty responses themselves — their root cause
is unmeasured (#3698's anyio cancel-scope is a candidate) — it only changes
what happens *after* one is detected. The `router_empty_response_detected`
audit event fires regardless of this setting.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `empty_stop_retry` | bool | `false` | Resend one self-describing continuation nudge (`role="system"`, #5273/#5274 — not a bare `"resume"`) when the router loop detects an empty `finish_reason="stop"` response. |

### `chat.theme`

Override the interactive TUI's Textual theme name (#4840 ③ — the
config-knob half; the colour direction itself was decided separately and
already shipped, #4869/#4875). Default (unset) keeps reyn's own
full-colour theme (registered name `"reyn"`). Any name Textual resolves
is accepted — reyn's own theme, or any of Textual's built-ins (`nord`,
`dracula`, `gruvbox`, `catppuccin-mocha`, `textual-dark`, `ansi-dark`,
…). Not validated at config-load time — an unknown name raises where
Textual itself resolves the theme, not here, so this config layer never
carries its own copy of Textual's theme registry to keep in sync.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `theme` | str \| null | `null` | Textual theme name for the interactive TUI. `null` keeps reyn's own default (`"reyn"`). |

### `chat.stream_repaint_min_interval`

The minimum wall-clock gap, in **seconds**, between two repaints of the
same streamed reply in the TTY conversation pane (#3570's repaint budget,
made reachable). Deltas arrive at the provider's rate — measured up to
~1000/s through a proxy that packs many SSE events into one read — and
each repaint costs a present plus a strip render of the whole accumulated
body, so painting every delta spends the loop redrawing frames no eye
separates.

The default is the knee measured on one terminal (2000 deltas, 60 KB
reply): `set_item` 1979 → 75, wall-clock 16.1 s → 3.3 s. That measurement
is the reason this knob ships with the value it does — and the reason the
knob exists: a slower terminal (SSH, a multiplexer, a remote desktop) has
a different knee, and before this field the only way to reach it was to
edit source.

Seconds, not the milliseconds its `events:` sibling (`agent_delta_coalesce_interval_ms`) uses: the measured value is `1/30`, and a whole-millisecond field would round it — moving the shipped default as a side effect of picking a unit. Do not "unify" the two without changing the default deliberately.

Raising it trades update smoothness for loop time. It can never lose
text: the reply's accumulation is unconditional, and a catch-up timer
bounds every deferral to one interval. A non-positive value falls back to
the default instead of disabling the budget — `0` means "repaint on every
delta", the pre-#3570 behaviour the measurement above exists to keep
operators out of.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `stream_repaint_min_interval` | float | `0.0333` (1/30) | Seconds between two repaints of the same streamed reply. Non-positive falls back to the default. |

### `chat.reasoning` fields

Capture of the provider `reasoning_content` is **always-on**; these knobs gate
what happens afterwards. Both `continuity` and `display` default **on**.

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `continuity` | bool | `true` | Persist reasoning to history **and** replay the recent turns' reasoning into the next turn's system prompt (cross-user-turn reasoning continuity, a text-section mirroring `act_turn_reasoning`). Opt-out to disable persist + replay. |
| `display` | bool | `true` | Surface reasoning in the UI (TUI + web, collapsible). Opt-out to hide it. Independent of `continuity`. |
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
    chain_seconds: 60          # wait for delegate reply before upstream error
    mcp_probe_seconds: 5       # per-server MCP tools-list probe timeout (#3475)
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
    capability_narrowing: off  # OPT-IN capability narrowing while external content is live: off | turn | iteration
  spawn:
    max_depth: 10                     # max LLM spawn-lineage chain depth (spawn_agent); 0 = unlimited
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
| `safety.timeout.chain_seconds` | float (s) | `60` | — | How long a multi-agent chain waits for a delegate reply before synthesising an error. `0` = disabled. |
| `safety.timeout.mcp_probe_seconds` | float (s) | `5` | — | Per-server timeout for the MCP tools-list probe (`ensure_mcp_tools_cached` / `reyn mcp refresh`, #3475). A server slower than this is **not cached at all** (#3520 — a timed-out probe produced no answer, so nothing is stored; it used to be stored as an empty tool list, which the model read as "this server has no tools" for the rest of the session and across restarts) and is re-probed on the next turn; an `mcp_tool_probe_degraded` audit-event names the server and reason (`timeout` / `exception`). Raise under co-located CPU load — that is also how you stop paying a re-probe every turn on a legitimately slow server. |

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
| `safety.threat_scan.capability_narrowing` | string | `off` | **OPT-IN** (#3501). The CAPABILITY half of the same defense: while `external_source`-tagged content is live in the active context, apply the `_untrusted` capability profile (deny memory-write / re-delegation / exec / MCP-, skill-, pipeline-install / spawn / pipeline-run). One ordered ladder, not an enable flag plus a granularity flag. `off` (default): the narrowing never engages — an agent keeps the capabilities it started the session with, whatever enters its context. `turn`: resolved at each turn boundary, so external content arriving mid-turn narrows from the NEXT turn. `iteration`: additionally re-resolved at every router-loop iteration, so content arriving in round N narrows round N+1 of the SAME turn (closes the same-turn injection window) at the cost of a legitimate external→privileged flow being narrowed mid-flow; monotonic within the turn (a turn-scoped latch survives a compaction evicting the tainted entry, so the taint cannot be laundered) and emits an `untrusted_narrowing_engaged` audit-event the first time it engages in a turn. An invalid value is rejected at config load rather than silently resolving to a level you did not ask for. |

### `safety.spawn` fields

Operator bounds on the LLM spawn tree — a DoS guard so an agent cannot mint an unbounded org. Set in `reyn.yaml` (the restart-only OUT layer): an LLM has no runtime path to raise its own base limit. Enforced at the LLM spawn **seams** (`spawn_agent`, `create_topology`); the operator CLI create path is unbounded (authority). Defense-by-default (non-zero) — there is no backward-compat spawn tree to break.

When a spawn would exceed a limit, the `safety.on_limit` checkpoint fires — the same mode-driven framework used by loop and budget caps:

- **`interactive`** (default): prompts the operator for approval to extend. On approval, the extension is recorded per-spawner so the same scope does not re-prompt. The base config limit is unchanged; any extension is operator-approved, never LLM-driven.
- **`unattended`**: rejects immediately (no prompt possible — CI / scripted runs).
- **`auto_extend`**: auto-approves up to `auto_extend_times` extensions, then rejects.

`max_depth` and `max_children` carry **separate per-spawner extension keys** so an operator-approved increase in one does not silently widen the other.

| Path | Type | Default | Description |
|------|------|---------|-------------|
| `safety.spawn.max_depth` | int | `10` | Maximum spawn-lineage chain depth (operator-top = 0; each `spawn_agent` +1). Exceeding this fires the `safety.on_limit` checkpoint. `0` = unlimited. |
| `safety.spawn.max_children` | int | `20` | Maximum fan-out: governs BOTH the direct spawn-children per parent (`spawn_agent`) AND the member count of a `create_topology`d topology (org size). Exceeding this fires the `safety.on_limit` checkpoint. `0` = unlimited. |
| `safety.spawn.max_pipeline_fan_out_depth` | int | `5` | Pipeline fan-out NESTING bound: the max depth of nested `for_each` scopes (a top-level `for_each` = 1; a `for_each` inside another's `do`/`collect` = 2; …). A `for_each` exceeding this FAILS the step (bounded-by-construction; no `on_limit` prompt — pipeline runs are non-interactive). Distinct from `max_depth` (spawn lineage): a pipeline agent-step carries no lineage, so `max_depth` does not cover fan-out. `0` = unlimited. |
| `safety.spawn.max_pipeline_spawns` | int | `100` | Pipeline spawn-COUNT bound: the max ephemeral sessions ONE pipeline run may spawn across all its `agent` steps (top-level or fanned out via `for_each`). A per-run monotonic counter; the spawn past the cap FAILS the step. The ONLY spawn-count enforcement for lineage-less pipeline agent-steps (`max_children` does not cover them). `0` = unlimited. |

See [`safety.on_limit` fields](#safetyon_limit-fields) for the mode settings.

## `tool_use` block

Chat-layer tool-use **scheme x transport** selector (FP-0066 P4b, #3247). Tool-use
decomposes into two orthogonal axes: `scheme` is the **presentation** — how
capabilities are shown/discovered to the LLM (`category` / `enumerate-all` /
`retrieval`) — and `transport` is how the model expresses the chosen action
(`tool_calls` / `content_fence`). The resolved `(scheme, transport)` pair
selects a registered `ToolUseScheme` — a pluggable mechanism for how tools
are presented to and dispatched from the LLM.

```yaml
tool_use:
  scheme: enumerate-all       # default
  transport: tool_calls       # default
  universal_wrappers_enabled: true    # default; set false to opt out
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `scheme` | string | `enumerate-all` | Presentation for the top-level chat layer: `category` / `enumerate-all` / `retrieval`. **Default `enumerate-all`** — flat-lists actions so the LLM invokes them directly instead of hallucinating `invoke_action` names (raised direct tool-use ~30%→100%). Set to `universal-category` for a minimal-surface / many-tool catalog (discover-then-call). |
| `transport` | string | `tool_calls` | How the model expresses a chosen action: `tool_calls` (native tool-calling) or `content_fence` (the action is expressed as fenced code in the reply text — CodeAct). |
| `universal_wrappers_enabled` | bool | `true` | **#4552 PR-3 — moved here from `action_retrieval.universal_wrappers_enabled`** (architect's ruling: a `tool_use`/presentation-scheme property, not a retrieval setting). For a layer whose `scheme` resolves to `universal-category`, `true` (default) exposes only the 4 universal wrappers (`list_actions`, `search_actions`, `describe_action`, `invoke_action`) in that layer's `tools=`.  Legacy per-kind tools (`invoke_skill`, `call_mcp_tool`, etc.) are no longer surfaced to the LLM on that layer but remain available as wrapper backing handlers.  `search_actions` is gated separately by [`embedding.enabled`](#embedding-block) (#4564 — this flag has NO effect on `search_actions` visibility in any scheme).  Set `false` to disable the wrapper surface entirely for that layer (= legacy tools become the only addressing path again).  Does not affect a layer whose `scheme` is `enumerate-all`/`retrieval` — those never consult this flag. Setting it explicitly `true` while `scheme` isn't `universal-category` has no effect; `reyn config validate` reports that combination (#4231(C)). |

See [Concepts: universal catalog](../../concepts/tools-integrations/universal-catalog.md) for the full `list_actions` / `describe_action` / `invoke_action` wrapper semantics (category discovery, error-recovery `suggestions`, weak-model landing design).

Every combination of the axis values above is implemented today:

| `scheme` \ `transport` | `tool_calls` | `content_fence` |
|---|---|---|
| `category` | `universal-category` | code-API over the wrappers |
| `enumerate-all` | `enumerate-all` (default) | CodeAct |
| `retrieval` | `retrieval` | search-first code-API |

A pair that is not in this table — a `scheme` or `transport` name reyn does not
have — raises a legible error at config-parse time rather than silently falling
back or being accepted. CodeAct is reached via `scheme: enumerate-all` +
`transport: content_fence` — it is the same full flat catalog as
`enumerate-all`, expressed as fenced code instead of native tool calls, not a
`scheme` name of its own. `retrieval` additionally requires `embedding.enabled:
true` (FP-0066 §7).

`scheme: category` + `transport: content_fence` gives the small-surface
counterpart of CodeAct: the model writes fenced Python, but the functions it is
shown are the catalog **wrappers** (`list_actions` / `describe_action` /
`invoke_action`) plus the base tools — so the system prompt does not grow with
the catalog the way CodeAct's does. A call reads
`result = invoke_action(action_name="read_file", args={"path": "README.md"})`.

```yaml
tool_use:
  scheme: category
  transport: content_fence
```

Choose it when a weak / low-cost model does better writing code than emitting
JSON tool calls **and** the catalog is large enough that listing every action up
front costs too much — CodeAct (`enumerate-all` + `content_fence`) gives up the
second. Every in-code call still passes the same exclude + permission gate as the
equivalent JSON call, plus sandbox containment.

`scheme: retrieval` + `transport: content_fence` is the search-first code-API:
the functions the model is shown are `search_actions` / `describe_action` /
`invoke_action` plus the base tools, with **no** `list_actions` — discovery here
is a search, not a listing. A turn reads

```python
hits = search_actions(query="read a file")
result = invoke_action(action_name="read_file", args={"path": "README.md"})
```

```yaml
tool_use:
  scheme: retrieval
  transport: content_fence
  # requires embedding.enabled: true
```

It differs from the `tool_calls` retrieval cell in cost, not in paradigm: there,
narrowing takes a round trip (the OS re-presents the matched actions as a new
`tools=` payload, because a payload can only change between calls); here the
search result is an ordinary value inside the snippet, so the search and the call
can happen in one turn. Choose it over `category` + `content_fence` when the
catalog is large enough that browsing it by category is the wrong entry point and
you would rather the model describe what it wants. If the embedding index is not
ready yet, this cell falls back to listing the flat catalog rather than showing a
search that would return nothing.

The old single `tool_use.chat` key is **removed** (clean-break, no compat
alias). A reyn.yaml still carrying `tool_use.chat` fails loud at config-load
time naming the `scheme` / `transport` migration — it is never silently
ignored. A former `chat: codeact` becomes `scheme: enumerate-all` +
`transport: content_fence`; a former `chat: universal-category` becomes
`scheme: category` (`transport` stays at its `tool_calls` default) — `category`
is the presentation-axis name, and it resolves to the registered
`universal-category` scheme.

A scheme owns how the `tools=` payload is built, the SP tool-use instructions, how an LLM response is interpreted, and how it is dispatched — so swapping `scheme` / `transport` changes the whole tool-use loop for the chat layer without OS changes.

For what each scheme does and **when to choose which** (`enumerate-all` / `retrieval` / `CodeAct` vs the default), see [Tool-Use Schemes](../../concepts/tools-integrations/tool-use-schemes.md).

## `web_fetch` block

SSL settings for the `web_fetch` tool and the MCP package registry.

Renamed from `web.fetch:` (#4174 T4) — the old `web:` key conflated this
(the web_fetch TOOL's own settings) with the unrelated `reyn web` GATEWAY's
own settings (now `gateway:`, below). `web:` never named which of the two
an operator was reading or writing.

```yaml
web_fetch:
  verify_ssl: true     # true | false | omit (default: env-var chain)
  ca_bundle: /path/to/ca-bundle.pem   # optional custom CA bundle
  max_download_bytes: 10485760        # wire-byte ceiling (default 10MB)
  allow_private_ips: false            # SSRF: opt-in to private IPs (default deny)
```

Priority chain (highest first):

| Priority | Condition | Effective SSL config |
|----------|-----------|----------------------|
| 1 | `web_fetch.ca_bundle` set | Custom CA bundle file (`verify=<path>`) |
| 2 | `web_fetch.verify_ssl: false` | Disable SSL verification (`verify=False`) — **use only in controlled environments** |
| 3 | `web_fetch.verify_ssl: true` | Force SSL verification (`verify=True`) |
| 4 | Both unset | Fall through: `SSL_VERIFY` env var → `litellm.ssl_verify` → `SSL_CERT_FILE` → `True` |

`verify_ssl` and `ca_bundle` also apply to MCP registry HTTP calls (package install).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `web_fetch.max_download_bytes` | int | `10485760` (10MB) | Maximum response bytes `web_fetch` reads off the wire. A response whose `Content-Length` exceeds this is rejected before any body is downloaded; a chunked / unknown-length body is aborted once the stream passes the ceiling (status `too_large`). Guards against an unbounded-body memory blow-up from a hostile or runaway URL. `<= 0` or non-integer falls back to the default. |
| `web_fetch.allow_private_ips` | bool | `false` | SSRF opt-in. When `true`, `web_fetch` / `safe.http` may fetch **private** RFC1918/ULA addresses (enterprise internal-fetch). Link-local, cloud-metadata (`169.254.169.254`), and loopback are **always** denied regardless of this flag. HTTP redirects are re-validated per hop (both the host allowlist and the IP-deny), so an allowlisted host cannot redirect to an internal target. Also exported to the `REYN_FETCH_ALLOW_PRIVATE_IPS` env var so the safe.http subprocess and registry clients honor the same opt-in. |

> ℹ️ **#4274**: `web_fetch.*` now reaches every live chat session's
> `web_fetch` op execution (`SessionFactoryConfig.web_fetch_config` →
> `Session` → the router `OpContext`). Before this landed, the block parsed
> and validated clean but never reached a real `web_fetch` call — a
> pre-existing gap the #4174 T4 rename did not itself fix or worsen. An
> operator who set a non-default value (e.g. `verify_ssl: false` or
> `allow_private_ips: true`) and never noticed it doing nothing will now
> see it actually take effect.

## `gateway` block

The `reyn web` gateway's own settings — authentication model, WebSocket
inbound-frame ceiling, and per-surface mount overrides.

Split from `web:` (#4174 T4) — see `web_fetch` above for the other half
that key used to conflate.

```yaml
gateway:
  ws_max_size: 16777216                 # WS inbound-frame ceiling (default 16MB)
  default_design: coral                 # OpenUI host's default design slug (unset → env → first alphabetically)
  auth:
    token: my-shared-secret             # T3 cross-machine bearer token (required for a non-loopback bind)
    require_token_on_loopback: true     # also require the token on loopback TCP (secure default)
    tls_certfile: /path/to/cert.pem     # operator TLS cert (T3); omit → self-signed TOFU
    tls_keyfile: /path/to/key.pem       # operator TLS key (T3); set together with tls_certfile
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `gateway.ws_max_size` | int | `16777216` (16MB) | Maximum size (bytes) of a single inbound WebSocket frame the `reyn web` gateway accepts; a larger frame is rejected by the server before delivery. Pins the WebSocket frame ceiling explicitly instead of relying on the server library's implicit default, so the bound stays in place across server-library upgrades. Operators may tighten or loosen it. `<= 0` or non-integer falls back to the default. |
| `gateway.default_design` | str | `null` | The OpenUI host's default design slug, served by `GET /api/web/config` (#4317). Resolution priority: `REYN_WEB_DEFAULT_DESIGN` env var → this field → first available design alphabetically. Was `web.default_design` pre-#4174-T4; T4's split enumerated `ws_max_size`/`auth`/`surfaces` but dropped this field entirely, leaving it with no address in the typed schema for a full cycle — #4317 gave it one. **A genuine behavior change, not a pure bugfix**: the old key was read via a raw `yaml.safe_load` of `reyn.yaml` that bypassed the loader/schema entirely, so it kept resolving `web.default_design:` correctly the whole time despite the schema no longer knowing that key — an operator still on the old key now gets nothing here (falls through to the next priority level) instead of the silently-surviving old value. `reyn config validate`/`migrate` surface the rename via the `"web"` `RenamedKeyHint`, alongside `auth`/`ws_max_size`/`surfaces`. |
| `gateway.auth.token` | str | `null` | The gateway's cross-machine (T3) bearer token. A **non-loopback bind refuses to start** without it (fail-closed — closes the accidental-exposure hole). A loopback bind generates an ephemeral token at startup when this is unset (printed in the launch URL, Jupyter-style), so no gateway surface is ever left unauthenticated. The token gates **every** functional surface uniformly — the AG-UI chat routes, `/api`, `/a2a`, `/mcp`, and the resource-fetch routes — not the AG-UI surface alone. |
| `gateway.auth.require_token_on_loopback` | bool | `true` | When `true`, even loopback TCP connections must present the token (secure default — a shared multi-user host must not leave the browser loopback surface open). Same-machine UDS connections are authenticated by OS peer credentials and never need a token. |
| `gateway.auth.tls_certfile` | str | `null` | Operator TLS certificate (PEM) for a T3 network bind. When unset, a self-signed certificate is generated at startup and its SHA-256 fingerprint is printed for trust-on-first-use pinning. Must be set together with `tls_keyfile`. |
| `gateway.auth.tls_keyfile` | str | `null` | Operator TLS private key (PEM) paired with `tls_certfile`. Setting only one of the two is a startup error. |

**Transport tiers** (secure-by-default). The gateway identifies every connection: **T1** in-process (the operator's own process, no auth); **T2** same-machine cross-process over a UNIX domain socket (`reyn web --uds PATH`) identified by OS peer credentials, or loopback TCP as a fallback; **T3** cross-machine network, which requires `gateway.auth.token` and runs over TLS. An intervention answer is a permission grant, so an unauthenticated connection cannot answer.

### `gateway.surfaces`: per-surface opt-in/opt-out (FP-0058 P2)

`reyn web` hosts several surfaces on the one gateway process; each can be
independently enabled or disabled. **Secure-default**: AG-UI, the web UI
(OpenUI shell + `/web/designs`), the REST `/api` control plane, `/health`,
and the resource-fetch routes (`/agents/*/tool-results/*`) are **ON** — the
operator's own browser/CLI need them to function at all. **A2A** and **MCP**
are **OFF** by default — they are broad machine-integration ports (peer
agents / external LLM clients reaching into this process), so they require
explicit opt-in.

```yaml
gateway:
  surfaces:
    a2a:
      enabled: true   # opt in to the Agent2Agent JSON-RPC surface
    mcp:
      enabled: true   # opt in to the MCP-over-SSE surface
```

| Surface | Secure-default | What it hosts |
|---------|-----------------|----------------|
| `agui` | ON | The AG-UI SSE transport (chat, self-gated per-handler). |
| `webui` | ON | The OpenUI shell (`/`, `/static/*`) and `/web/designs/*`. |
| `health` | ON | `GET /health`. |
| `api` | ON | The REST `/api` control plane (agents / topologies / permissions / budget / web-config / web-data), auth-gated `operator` class. |
| `resources` | ON | `/agents/<agent>/tool-results/<artifact>`, auth-gated `resource` class. |
| `a2a` | **OFF** (opt-in) | The Agent2Agent JSON-RPC spine, auth-gated `peer` class. |
| `mcp` | **OFF** (opt-in) | MCP-over-SSE (`/mcp/sse`, `/mcp/messages`), auth-gated `client` class. |

Also settable per-surface from the CLI — `reyn web --enable a2a --enable mcp`
or `reyn web --disable api` (repeatable per-surface flags, not a comma-list).
**Precedence: CLI `--enable`/`--disable` > `gateway.surfaces` config > the
secure-default table above.** This is launch-time-only, operator-owned
config — read once when `reyn web` boots, never hot-reloadable and never
LLM-settable (the LLM has no launch authority over which surfaces this
gateway hosts). The webhook plugin surface (`webhooks.yaml`) is unrelated to
this table and keeps its own separate, pre-existing opt-in.

## `hooks` block

Agent-lifecycle hooks — a thin operator layer over the unified inbox
and the P6 lifecycle. A **list** of entries; each fires at a lifecycle point
or an external-event point (`on`), optionally narrowed by `matcher`, and
carries **exactly one** of four mutually-exclusive schemes:

- **`template_push`** — inject an attributed `[hook:<name>]` message from a config
  Jinja2 template.
- **`exec`** — run an argv as a pure side-effect (output IGNORED). Renamed from
  `shell_exec` in #3226 Phase 4 (naming honesty, not a security change — this
  scheme never ran `/bin/sh -c <string>`; it always executed a tokenized argv
  with `shell=False`). Payload is **argv-list-only** (a clean break from the
  pre-Phase-4 shell-command string — no compat alias).
- **`exec_capture`** — run an argv whose **stdout is a JSON push-directive**, pushed
  via the same path as `template_push` (the only difference is the directive's
  source: captured stdout vs a Jinja2 render). Renamed from `shell_push` in
  #3226 Phase 4, same argv-list-only payload.
- **`pipeline_launch`** — launch a registered [pipeline](../../concepts/runtime/pipelines.md)
  with input rendered from the event's template vars, async/detached.

Hooks never silently mutate tool results; pushes are new, attributed, evented
messages.

```yaml
hooks:
  - name: next_step              # optional → the [hook:next_step] attribution (absent → the point)
    on: turn_end                 # turn_start|turn_end|session_start|session_end|mcp_resource_updated|file_changed|cron_fired|webhook_received
    template_push:
      message: "Turn complete — consider the next step."
      wake: false                # false = passive context (C); true = start a turn (E)
      push_when: "true"          # optional Jinja2 → bool; false skips the push
  - on: session_start
    exec: ["touch", "/tmp/reyn-session-started"]   # argv only — no shell redirection (">>")
  - name: dynamic                # stdout decides whether/what/how to push
    on: turn_end
    exec_capture: ["scripts/decide-next.sh"]   # emits {"push_when":true,"wake":true,"message":"..."}
  - on: mcp_resource_updated      # external-event point — fired by a subscribed MCP resource
    matcher: {server: "github", uri: "file:///repo/docs/**"}
    pipeline_launch:
      name: reindex_docs
      input_template: {uri: "{{ uri }}"}
  - on: cron_fired                # external-event point — a message-based cron job fires
    matcher: {job_name: "backup"}
    exec: ["touch", "/tmp/reyn-backup-ran"]
  - on: turn_end                  # per-hook sandbox knobs (exec schemes only). The
    exec: ["npm", "run", "lint"]  # agent-level `sandbox.policy` does NOT reach a hook
    subprocess: true              # exec argv — these keys are where a hook's sandbox is set.
    network: true                 # omit any of them → that axis stays at the hook floor
    write_paths: ["/tmp/lint"]    # (no fork / no network / no writes)
  - on: webhook_received          # external-event point — an inbound webhook resolves to a session
    matcher: {transport: "slack"}
    template_push:
      message: "New Slack message routed in."
      wake: false
  - on: turn_end                  # `include` appends a field's raw value after `message`,
    template_push:                # fenced and never Jinja2-rendered (proposal 0067 P2)
      message: "Turn complete — the user's last message is attached below."
      wake: false
      include: ["user_text"]
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `on` | string | _required_ | Lifecycle point (`turn_start`, `turn_end`, `session_start`, `session_end`), an external-event point — `mcp_resource_updated` (a subscribed MCP resource changing), `file_changed` (a watched path changing, requires [`fs_watch`](#fs_watch-block)), `cron_fired` (a message-based cron job fires), or `webhook_received` (an inbound webhook resolves to a session) — or a `composed:<name>` composed-event kind (an OPEN namespace, one entry per [`composers:`](#composers-block) config's `emit.kind`; a composed→wake chain is bounded by the same `max_hook_driven_turns` loop-valve as any other hook-driven turn). `cron_fired`/`webhook_received` are non-blocking relative to their ingress — dispatch never delays the cron job's delivery or the webhook's HTTP response. See [Concepts: hooks § External-event points](../../concepts/runtime/hooks.md#external-event-points). |
| `name` | string | _the point_ | Optional operator label surfaced as the `[hook:<name>]` attribution prefix on a push. Absent → defaults to the hook-point (e.g. `[hook:turn_end]`). |
| `matcher` | map[string,string] | _none_ | Optional filter, evaluated against the firing event's template vars before the hook's action runs. Every named field must match: exact string equality, except `uri`/`path` (shell-style glob via `fnmatch`). Absent/empty → the hook always fires (unaffected for lifecycle hooks, which carry no `server`/`uri`/`path`). **Validated at load** for the builtin hook points: a matcher field name outside that point's builtin schema (e.g. a typo, or a lifecycle point's matcher naming `server`/`uri`) is a `HookConfigError` at config-load time, not a silently-dead matcher. A future/custom point with no builtin schema entry keeps the pre-validation behavior — a field the event doesn't carry never matches at runtime. |
| `template_push` | map | _none_ | Inbox-push hook from a Jinja2 template (one of the four schemes). `message` (Jinja2 → text), `wake` (bool/Jinja2, default `true`: `true` starts a new turn = self-continuation; `false` rides along with the next turn as passive context), `push_when` (Jinja2 → bool, default `true`; `false` skips), `session` (parsed + carried; naming a different session routes the push to that session's inbox — **cross-session push**; omitted or the current session → the local path), `include` (list of hook-event field names, default `[]` — each is appended to `message` VERBATIM, fenced and attributed, never through Jinja2; a name the firing event doesn't carry — a typo, or a field this hook point doesn't have — is written as `(absent)`). `message` may only interpolate `context_safe` fields (today, every builtin field is — `include` is the door for a future non-`context_safe` field to still reach the pushed text as content, without giving it template control flow). |
| `exec` | list[string] | _none_ | An argv run as a pure side-effect (one of the four schemes; renamed from `shell_exec` in #3226 Phase 4 — argv-list-only, a clean break from the pre-Phase-4 shell-command string). Non-empty list of non-empty strings. Sandbox-gated + consent-allowlisted; stdout/stderr are logs, never parsed. |
| `exec_capture` | list[string] | _none_ | An argv whose **stdout is a single JSON object** `{"push_when": bool, "wake": bool, "message": str, "session"?: str}` (first three required), pushed via the same path as `template_push`. Renamed from `shell_push` in #3226 Phase 4 — argv-list-only, same clean break as `exec`. stdout must be pure JSON (logs → stderr). Sandbox-gated + consent-allowlisted. **Rule, not an enumeration** (architect note, #5210 doc follow-up — a closed list of failure causes goes stale the day a new one is added; this doesn't): pushes ONLY when a well-formed directive is obtained from stdout — every other outcome skips, fail-safe, e.g. non-zero exit / invalid JSON / missing or wrong-typed field / a decoded-stdout token count exceeding a live context-budget-derived cap (#5210, rejected outright, never truncated — a cut JSON payload would fail to parse and be indistinguishable from a clean no-push run). |
| `subprocess` | bool | `false` (the floor) | **`exec` / `exec_capture` only** — may this hook's argv spawn child processes? Declaring it on another scheme is a `HookConfigError` (a silently-ignored security field would read as an applied restriction that was never applied), as is a non-bool value. Omitting the key keeps the `false` floor; only an explicit `true`/`false` is your expressed will. Set `true` when the command forks: a bare command that resolves to a version-manager shim (`pyenv`/`asdf`/`mise`) or a spawn-based launcher (`npx`/`uvx`) forks **internally**, so it is denied under the default even though the command itself never forks — the symptom is an opaque `fork: Operation not permitted` (now logged with `denial_class=fork_denied` naming it an environment/config problem). Using an absolute path to the real binary is the alternative. Unlike a stdio [MCP server's `subprocess:`](#mcp-servers) (default `true` — such a server *forks to exist*), a hook's fork need depends on your own command, so there is no safe blanket default: the judgment is per hook. Note the agent's own `hooks_add` tool can only create `template_push` hooks, so `exec`/`exec_capture` (renamed from `shell_exec`/`shell_push` in #3226 Phase 4) — and this knob — remain operator-owned. |
| `network` | bool | `false` (the floor) | **`exec` / `exec_capture` only** — may this hook's argv reach the network? Same rules as `subprocess` above: declaring it on another scheme or giving it a non-bool value is a `HookConfigError`, and omitting it keeps the `false` floor (only an explicit `true`/`false` is your expressed will). Set `true` for a hook that posts to an API or pulls from a registry. Note that the agent-level [`sandbox.policy`](#sandboxpolicy-sub-keys) does **not** grant this — see the boundary note under that block. |
| `write_paths` | list[string] | `[]` (the floor) | **`exec` / `exec_capture` only** — filesystem paths this hook's argv may write (`~` expanded; write implies read). Same rules as `subprocess` above; omitting the key keeps the floor, which grants **no** writes, while an explicit list — including `[]` — is your expressed will. Keep the scope tight: grant the specific directory the hook writes, never `~`. A hook shell's floor carries no sensitive-read deny-list by default (#3901 — `read_deny_paths`'s dataclass default is now empty; the MCP server default is a deliberate opt-in exception, not the hook floor). Not granted by the agent-level [`sandbox.policy`](#sandboxpolicy-sub-keys) — see the boundary note under that block. |
| `pipeline_launch` | map | _none_ | Launch a registered pipeline (one of the four schemes). `name` (required — the pipeline's registered name; unregistered → warns and skips the launch, the hook point still completes), `input_template` (optional — a `dict`'s string leaves are each Jinja2-rendered against the event's template vars; a plain string is rendered once and its output parsed as a JSON object; omitted → `input=None`). Async/detached: the result arrives later on this session's own inbox as a `pipeline_result` message. |

**Interpolating a project path — `${REYN_PROJECT_DIR}`, not an absolute
path.** `reyn.yaml`'s `hooks:` block (this layer only — `exec`/`exec_capture`
argv, `write_paths`) expands reyn's own `${REYN_PROJECT_DIR}` token to this
project's root before running. Prefer it over a hardcoded absolute path
for anything **inside** the project (`exec: ["${REYN_PROJECT_DIR}/scripts/
lint.sh"]`, `write_paths: ["${REYN_PROJECT_DIR}/build"]`) — the config then
still works if the project directory ever moves or is checked out
elsewhere. An absolute path stays correct here only for something
genuinely **outside** the project (a sibling repo, a system tool). `${REYN_
AGENT_NAME}` is **not** available at this layer — this file is read once,
project-wide, before any agent is resolved, so there is no per-agent value
to supply; a per-agent value belongs in that agent's own `.reyn/agents/
<name>/hooks.yaml` instead. A `${REYN_AGENT_NAME}` left in `reyn.yaml`
itself resolves to an empty string with a warning naming it explicitly
(#5351) — do **not** silence that warning by exporting
`REYN_AGENT_NAME` as an environment variable; that pins the whole shared
config to whichever agent's name happens to be in the process's
environment, with no further signal.

**`wake` / `push_when` truthiness, and why a typo fails differently
depending on the field.** A rendered `wake`/`push_when`/`session` string
is converted to bool by case-insensitive lookup: `true`/`1`/`yes`/`on` →
`True`; `false`/`0`/`no`/`off`/empty-string → `False`. Any other value is a
render error. `message` uses strict undefined-variable checking — a
typo'd variable name raises, and the whole push is skipped (loud
failure); `wake`, `push_when`, and `session` use silent checking — a
typo'd variable there renders as an empty string, which resolves to
`False`/`None` (quiet, fail-safe: don't wake, don't push, or fall back to
the current session on a broken condition). Any render failure at all —
bad Jinja2 syntax, an unrecognised truthiness string, or `message`'s
strict-undefined error — is caught: the push is skipped
(`push_when=False`) and logged at WARNING, never crashes the turn.

## `composers` block

Event correlation (Hook-Event Redesign Phase 4b/5, proposal
[0059](../../deep-dives/proposals/0059-hook-event-redesign.md) §5/§9). A
**Composer** subscribes to this session's per-process `HookBus`
(independent pub/sub broadcast of every hook-event — NOT the P6
audit-event/WAL-event stream), buffers matching events per its `op`, and —
once the op's condition is met — publishes ONE new event with
`kind = "composed:<name>"` back to the same bus. A `composed:<name>` kind
is then a normal `hooks:` `on:` target ([`hooks` block](#hooks-block) above)
— subscribing a Sync side-effect to the composition's output.

```yaml
composers:
  - name: deploy_approved
    op: all
    inputs:
      - { kind: builtin:external:mcp_resource_updated, match: { server: "github" } }
      - { kind: mcp:approval-server:approved }
    policy: { capacity: 10, overflow: reject, ttl: 5m }
    emit: { kind: composed:deploy_approved }

  - name: job_overdue
    op: deadline
    on: mcp_resource_updated
    matcher: { uri: "orch://job/*/started" }
    until:
      on: mcp_resource_updated
      matcher: { uri: "orch://job/*/done" }
    correlate_by: job_id
    ttl: 1800
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `name` | string | _required_ | The composer's identifier — also the correlation-key namespace and the `composer_fired`/`composer_dropped` P6 event's `composer` field. |
| `op` | string | _required_ | One of `all` (every input arrives, per key), `any` (first matching input, stateless), `seq` (inputs' kinds arrive in the configured order), `window` (fires `ttl` seconds after the first matching event, with everything buffered), `debounce` (fires `ttl` seconds after the last matching event with no newer one in between), `correlate_by` (like `all`, keyed by a payload field), `count` (fires once `count` matching events arrive, per key), `deadline` (issue #3166 — fires when its `until` pattern does NOT arrive within `ttl` of its `on` pattern, per key; see below). |
| `inputs` | list[map] | _required_ (all ops except `deadline`) | Each entry: `kind` (a hook-event kind — a builtin `builtin:lifecycle:*`/`builtin:external:*` kind, or any other kind observed on the bus) + optional `match` (a payload field→pattern filter, same semantics as a hook's `matcher`). `source` is NOT settable — every bus event carries `source="builtin"` (kind + payload already encode the source type/instance); naming any other `source` value is a load-time error. |
| `on` / `matcher` | string / map | _required for `op: deadline`_ | `deadline`'s arm pattern, in place of `inputs` — `on` is the kind, `matcher` the optional payload filter (same shape as `inputs[].kind`/`match`). |
| `until` | map | _required for `op: deadline`_ | The disarm pattern — `{on, matcher}`, same shape as the top-level `on`/`matcher`. If `until` does not arrive for a given key within `ttl` of `on` arming that key, the composer fires; if it arrives first, the key is silently disarmed and never fires. |
| `policy` | map | `{capacity: 10, overflow: drop_oldest, ttl: 5m}` | `capacity` (max concurrent pending correlation keys), `overflow` (`drop_oldest`/`drop_newest`/`reject` — no publisher-blocking backpressure; also bounds a `deadline` arm-storm), `ttl` (seconds, or a `<N><unit>` string with unit `s`/`m`/`h`; an incomplete `all`/`seq`/`correlate_by`/`count` pending record older than `ttl` is evicted — for `window`/`debounce`, `ttl` IS the fire timer, and for `deadline`, `ttl` is the deadline itself). For `deadline` only, `ttl` may also be given at the top level (as in the example above) instead of nested under `policy:`. |
| `correlate_by` | string | _none_ | The payload field read as the correlation key (instead of one global bucket). Required when `op: correlate_by`; optional but typical for `op: deadline` (e.g. keying a dead-man monitor by `job_id` so N jobs are watched independently). |
| `count` | integer | _none_ | Required when `op: count` — the threshold of matching events before firing. |
| `emit.kind` | string | _required_ (defaults to `composed:<name>` for `op: deadline` if omitted) | The composed event's kind — MUST start with `composed:` (namespace-enforced at load time; collides otherwise with the P6 audit-event surface). |
| `durable` | boolean | `true` for `op: deadline`, `false` otherwise | Whether this composer's pending state must survive a process crash. `true` routes it to a full-state snapshot file (`<per-session state dir>/composer_pending.json`, one atomic rewrite + fsync per pending change) and an armed key comes back with its ORIGINAL arm instant; `false` keeps the free in-process dict. Setting `durable: false` on a `deadline` composer is allowed but emits a load-time `UserWarning` — a dead-man switch that dies with its process is never a silent posture. |

**`deadline` — the dead-man op.** Every other op fires because something
*happened*; `deadline` is the only one that fires because something did
**not** happen within its `ttl` (a heartbeat that stopped, an approval that
never came, a job that never finished). A `deadline` fire is not itself an
error signal — it means "the thing I was waiting for is missing", and its
composed payload always carries `armed_at` (when the key armed),
`ttl`, and `awaited` (the `until` pattern that did not arrive) so an
operator can see *why* it fired without re-deriving it from raw hook-events.
Internally `deadline` is not new machinery: it reuses the exact same per-key
`PendingStore` + TTL sweep + `QueuePolicy` + `correlate_by` every other op
uses — the only change is that the sweep FIRES an expired pending record
instead of silently discarding it.

**`deadline`'s reliability posture is stricter than every other op's**, which
is why it is the one op that defaults to `durable: true`. For the other seven
ops a crash drops one buffered notification; for `deadline` it drops the
dead-man monitor itself, at the same time as (and likely for the same reason
as) whatever it was watching. With the default in place, an armed `deadline`
is restored after a restart **with its original arm instant**, so a deadline
missed during the downtime fires on the first sweep rather than silently
restarting its clock.

**Reliability posture: best-effort by default, durable where it matters.** A
Composer's in-flight correlation state is held in memory unless `durable:
true` (see the key table above), so a partially-matched
`all`/`seq`/`correlate_by` simply never fires after a crash — a deliberate
scope decision (the Bus itself is already lossy under backpressure, so a
Composer built on it cannot promise more). A `durable` composer instead keeps
its pending set in `<per-session state dir>/composer_pending.json`, a
full-state snapshot file rewritten atomically on every change and never
derived from the WAL — so it survives WAL truncation structurally (CLAUDE.md's
recovery-feature gate). Every fire emits `composer_fired`; every drop
(overflow or `ttl`-eviction) emits `composer_dropped` — both metadata-only
(composer name + correlation key + reason, never the buffered payload
content).

**The composed→wake loop-valve bound.** A `composed:<name>` hook's
wake=true push traverses the exact same inbox `kind="hook"` path any other
hook-driven wake does, so a self-stimulating composed→wake chain (a
composer whose input is fed by a lifecycle point its own consumer hook's
next turn re-triggers) is bounded by the session's existing
`max_hook_driven_turns` cap with zero additional bounding logic — see
[Concepts: hooks § Async Bus and Composer](../../concepts/runtime/hooks.md#async-bus-and-composer-event-correlation).

Composers are read from the SAME 4-layer additive combine as `hooks:`
(`reyn.yaml` startup ∪ `.reyn/config/hooks.yaml` runtime ∪ per-agent ∪
per-session) but are **startup-only** — added/removed composer entries take
effect on the next session start, not via the hooks hot-reload seam (a live
Composer's in-flight `PendingStore` correlation state has no analogous
reload-time reconciliation yet).

## `fs_watch` block

Operator-declared filesystem paths whose changes fire the `file_changed`
[external-event hook](../../concepts/runtime/hooks.md#file_changed) (#2608 H4).
Each path is watched recursively. To scope a hook to a sub-tree, give it
`on: file_changed` plus a `matcher: {path: "..."}` glob — see the `hooks:`
block above.

Restart-only (OUT-set) — there is no op or tool verb that lets an agent
register or widen a watch; a filesystem-wide change feed is treated as the
same class of concern as sandbox policy.

```yaml
fs_watch:
  paths: ["src", "docs"]
  debounce_seconds: 0.2   # optional
```

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `paths` | list[string] | `[]` | Directories watched recursively for create/modify/delete events. Empty (the default) → the watcher never starts, byte-identical to a build with no `fs_watch:` config. |
| `debounce_seconds` | float | `0.2` | A burst of events for the same path within this window coalesces into a single `file_changed` fire (one logical change = one hook fire, not one fire per underlying filesystem event). |

Requires the `watchdog` package: `pip install reyn[fs-watch]`. `paths`
configured without the extra installed logs a warning once and disables the
watcher for that session — the rest of the session is unaffected.

## `sandbox` block

Backend selection, unsupported-platform policy, and the agent-level sandbox
policy for `sandboxed_exec` ops + the OS's in-process file/http gates.

```yaml
sandbox:
  backend: auto          # auto | seatbelt | landlock | noop
  on_unsupported: warn   # warn | error | ignore
  mode: compat           # compat | strict (#3823) — mode sets only the
                          # DEFAULT for a policy key left unset below; an
                          # explicit key here always wins over mode. Not
                          # "custom" or "off" — see #3823 for why.
  policy:                # optional — the agent-level (operator) sandbox
                          # policy, config vocabulary (#3823, decoupled from
                          # SandboxPolicy's internal field names)
    network: true
    subprocess: true
    allow_write_paths: ["{{workspace}}", "/tmp"]
    deny_write_paths: []
    deny_read_paths: []
    allow_env_names: null   # unset = deny-list-only; a list SWITCHES the
                             # env axis to allow-list semantics
    deny_env_names: []
    timeout_seconds: 600
  require_capabilities: []  # #4935 — opt-in only, empty = no run affected
```

> ℹ️ **Every axis except `allow_write_paths` defaults to full compat** (owner
> ruling, #3901; config vocabulary per #3823): `network`/`subprocess`/
> `deny_read_paths`/`deny_write_paths`/`deny_env_names` all start at "nothing
> extra restricted" — the sandbox's job is bounding what happens **behind** a
> permitted action, not re-deciding what the launching shell could already do.
> `allow_write_paths` is the one field that stays closed by default: it is the
> operator-unknowable value ("this op needs this directory") the kernel backend
> consumes directly, so there is no safe compat default to fall back to. Setting
> `mode: strict` flips every axis except `allow_write_paths` (and read, which
> has no mode-based default at all) to its closed default — see the `mode` row
> below.
>
> **A `deny_write_paths` entry always wins over an overlapping `allow_write_paths`
> grant** (and `deny_read_paths` likewise over the broad read surface,
> independently — the two axes are separate fields, each denying only its own
> axis, #3901). On the Seatbelt backend the deny rules are emitted **after** the
> `allow_write_paths` allow rules, and SBPL is last-match-wins — so a broad
> `allow_write_paths` entry (e.g. `~`, `$HOME`, `/`) that engulfs a path listed in
> `deny_write_paths` does **not** open it for writing, and the OS emits a
> `sandbox_policy_narrowed` audit-event so the narrowing is visible (#2978). If
> you want a credential location (`~/.ssh`, `~/.aws`, `~/.gnupg`, etc.)
> protected, list it explicitly in `deny_read_paths` (and `deny_write_paths` if
> you also want the write axis covered) — unlike before #3901, this is no
> longer a default; an operator who wants it back opts in. Still scope
> `allow_write_paths` to the narrowest directories the process actually needs.

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `backend` | string | `auto` | Enforcement backend. `auto` lets the OS pick: macOS < 26 → `seatbelt` (sandbox-exec SBPL), Linux ≥ 5.13 with `sandbox-linux` extra → `landlock` (+ optional seccomp-BPF), otherwise → `noop` (audit-only, no enforcement). Explicit values force a specific backend. |
| `on_unsupported` | string | `warn` | Policy when **no OS sandbox backend is available** — whether an explicit `backend` was forced-but-unavailable, `backend: auto` found no platform backend (the auto path honors this too), OR the selected backend **failed its enforcement self-test** (it is present but did not deny — such a backend is treated exactly like an absent one). `warn` logs a WARNING at selection and falls back to `noop` (default — not silent). `error` raises `RuntimeError` (**fail-closed** — refuse to run AI-generated code unsandboxed; set this where enforcement is required, and it works with the default `backend: auto` and against a present-but-inert backend). `ignore` silently falls back. |
| `mode` | string | `compat` | #3823: which DEFAULT the resolved policy uses for a `policy` key the operator left **unset** below — never a key the operator wrote explicitly (an explicit `allow_X`/`deny_X` or bare bool always wins over `mode`). `compat` (default) leaves every axis at its compat default (nothing blocked; audit/events/timeout/cancel-teardown still apply). `strict` flips network/subprocess/env to their closed default (network off, subprocess denied, env passes nothing) — `allow_write_paths` is UNAFFECTED by mode (stays the caller-supplied per-op workspace floor, operator-unknowable) and read has no mode-based default at all (no `allow_read_paths` concept, #1199 — only an explicit `deny_read_paths` narrows either mode). Allowed: `{compat, strict}` — not `custom` (owner ruling: was never a third direction, was the symptom of `mode`/`policy` having no composition rule) and not `off` (expressible as `compat` with every axis at its compat default). |
| `policy` | map | _none_ | **Agent-level (operator) sandbox policy, in the config vocabulary below (#3823).** When set, it is the deterministic policy applied to sandboxed ops **and** folded into the `SandboxLayer` of the permission intersection (`∩`) for the OS's in-process file/http gates, on the `network`/`subprocess`/`env` axes — **winning over** op-declared fields, so a skill or the LLM cannot widen it. `allow_write_paths` (and the read/write deny-lists) do NOT participate in that intersection: an operator cannot know in advance what directory an op needs, so the kernel backend consumes them directly (#3901 PR-B ③). Omitted (the default) means **no agent-level restriction**: the `SandboxLayer` stays the identity (`⊤`) and op-level fields govern, exactly as before. Sandbox authorization is an operator/run concern. See sub-keys below. |
| `require_capabilities` | list of string | `[]` | **#4935, opt-in only.** Named-service capability classes (declared, never probed — see `reyn.security.sandbox.capability`'s own module docstring) the operator requires the RESOLVED backend to support. Today the only known name is `ipc_named_service` — e.g. Seatbelt's `com.apple.SecurityServer` grant (#4937), which `gh` needs. `dscl`/`scutil` need OTHER named services under this SAME category that are **not** granted today — requiring this capability does **not** make them work; it only rejects a backend with no mechanism at all (noop/landlock resolve to NOT_SUPPORTED; Seatbelt always resolves to SUPPORTED regardless of which specific service you actually need, since the declaration is per-category, not per-service — see `reyn.security.sandbox.capability`'s own module docstring for exactly which services are and aren't granted today). An unrecognised name raises at config-load time. Empty (the default) means this field changes nothing — no run is affected unless you name a capability. When the resolved backend does NOT support a required capability, `sandbox.on_unsupported` (the SAME 3-way knob above, not a new vocabulary) governs the response. `reyn doctor` discloses each declared backend's own support under the sandbox posture section (C-5). |

### `sandbox.policy` sub-keys

When `sandbox.policy` is present, these are the config-facing vocabulary
(#3823) — `<direction>_<axis>_<unit>` word order for a path/name-set axis,
bare `<axis>` for a bool axis, decoupled from `SandboxPolicy`'s own internal
field names/senses. Unknown keys are rejected at config load (never silently
dropped — a dropped key on a security deny-list would read as "nothing to
deny").

| Key | Type | Default | Description |
|-----|------|---------|-------------|
| `network` | bool | `true` (compat, #3901); `false` under `mode: strict` | Allow outbound network from the sandboxed process. Still participates in the permission intersection alongside `subprocess`/env (an operator-declared value, not a workspace floor) — a config-allowed host is still denied under `network: false` (bypass-prevention, #1199 S3.1c-2). Set `network: false` explicitly to isolate the process. |
| `subprocess` | bool | `true` (compat) — positive framing, `true` = allowed; `false` under `mode: strict` | Whether the process **may** spawn children (owner decision 2026-07-22, #3202: a UX-blocking axis is opt-in restricted, not deny-by-default). Set `subprocess: false` to deny `process-fork`; enforced. |
| `allow_write_paths` | list[string] | `[]` | Filesystem paths the process may write (tight guard) — the one axis that stays closed by default and is UNAFFECTED by `mode` (operator-unknowable per-op value, no compat floor to fall back to). Write implies read for these paths. A `deny_write_paths` entry that overlaps a grant here still wins (deny-always-wins, #2978). `~` is expanded. |
| `deny_read_paths` | list[string] | `[]` (compat, #3901) | Sensitive paths to DENY from the broad read surface (defense-in-depth, **opt-in**) — empty by default; before #3901 this defaulted to 7 OS-level credential locations (`~/.ssh`, `~/.aws`, `~/.gnupg`, `~/.config/gcloud`, `~/.kube`, `~/.docker/config.json`, `~/.netrc`), now an operator sets that list explicitly to get it back. No mode-based default (there is no `allow_read_paths` concept, #1199) — `strict` cannot narrow reads any tighter than `compat`; only an explicit `deny_read_paths` narrows either mode. Enforced only on backends that support deny-after-allow rules (Seatbelt); not enforceable on allowlist-only backends (Landlock, which has no read-deny primitive). |
| `deny_write_paths` | list[string] | `[]` | The write axis's own deny-list (#3901), mirroring `deny_read_paths`. An `allow_write_paths` entry that overlaps or is a superset of any entry here does **not** defeat the deny — the deny always wins on Seatbelt (#2978), and a `sandbox_policy_narrowed` audit-event records the narrowing. Denies ONLY the write axis. |
| `allow_env_names` | list[string] \| `null` | `null` (deny-list-only); `[]` under `mode: strict` | New (#3823): SWITCHES the env axis to allow-list semantics when set to a list — only those names pass through (still intersected with `deny_env_names`, deny always wins). `null`/omitted keeps the deny-list-only behavior below. |
| `deny_env_names` | list[string] | `[]` (compat, #3901) | Env-var names to WITHHOLD from the sandboxed process. Empty (the default) means the WHOLE environment passes through, same trust level as the launching shell; list a name here to withhold it specifically. Ignored on the axis once `allow_env_names` is set as a list (deny is still intersected on top, not bypassed). |
| `timeout_seconds` | int | `120` (#3903①, 2026-08-11 — was `60`) | The foreground wall-clock timeout used when the LLM's `exec` call doesn't request an override. Must not exceed `max_timeout_seconds`, or config load fails. |
| `max_timeout_seconds` | int | `600` (#3903①) | The ceiling the LLM's per-call `timeout` request (`exec`'s optional `timeout` arg) is checked against — **operator-controlled**, never a hardcoded value: a request above THIS number is rejected (not silently reduced), naming the actual configured max. Narrowing it below `600` genuinely narrows what the LLM can ask for; the LLM can never widen it. |
| `max_output_bytes` | int | 10 MiB | Per-stream cap on captured stdout/stderr — output beyond it is drained-and-discarded (the `truncated` flag is set). |

> ⚠️ **`sandbox.policy` does not apply to shell hooks.** It governs sandboxed ops and
> the OS's in-process file/http gates; a [hook shell](#hooks-block)'s sandbox is scoped
> **per hook**, by that hook's own `subprocess:` / `network:` / `write_paths:` keys. This
> is deliberate — a hook is a small declarative reaction to a lifecycle event, so "no
> fork, no network, no writes" stays the right floor for it even in a run whose *ops*
> are deliberately unsandboxed. It is **not** silent: if you declare one of those three
> axes here and a shell hook does not re-declare it, the OS logs a WARNING naming the
> per-hook key that reaches it and emits a `sandbox_policy_not_applied` audit-event
> (#3005). Setting the key on the hook — to any value — is your decision on that axis
> and stops the warning.

See [Reference: control-ir — `sandboxed_exec`](../runtime/control-ir.md#sandboxed_exec) for the op schema and backend selection details.

## `agent_id`

Runtime agent identity for audit trail and HTTP header propagation.

```yaml
agent_id: "reyn/acme/code-review-agent"  # default: reyn/<hostname>
```

A plain top-level scalar (#4174 T5 — flattened from the old `agent: {id:
...}` namespace: that block held exactly one field, so the namespace added
indirection without adding structure).

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `agent_id` | string | `reyn/<hostname>` | Stable identifier for this Reyn instance. Stamped onto every P6 event payload as `agent_id` and injected into outgoing MCP, A2A, and external HTTP requests as the `X-Reyn-Agent-Id` header (SOC2 / ISO27001 / METI v1.1 audit pattern). Recommended format: `reyn/<org>/<role>` (operator-defined). An empty string falls back to the default so leaving the field blank does not emit an empty `agent_id` into events or headers. |

The default `reyn/<hostname>` gives a fresh install a usable identity without operator action. Override in `reyn.yaml` when running multi-agent fleets or enterprise deployments that need a stable per-role identifier.

See [Concepts: multi-agent — Agent ID propagation](../../concepts/multi-agent/multi-agent.md) for cross-agent tracing and A2A header forwarding.

## `observability` block

Opt-in OpenTelemetry (OTLP) export of the P6 audit-event stream to spans,
metrics, and log records. **Off by default** — with no endpoint the exporter is
never attached and behavior is byte-identical to having no OTEL. It is a lossy,
fire-and-forget downstream: it never writes to `.reyn/events` or the WAL, so
recovery and replay are independent of it.

```yaml
observability:
  otel:
    endpoint: "http://localhost:4318"     # OTLP HTTP base URL; "" disables
    headers:
      Authorization: "Bearer ${OTEL_TOKEN}"
    service_name: "reyn"
    capture_content: false                # SR3: raw prompt/response OFF by default
```

### `observability.otel` fields

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `otel.endpoint` | string | `""` | OTLP HTTP base URL (e.g. `http://localhost:4318`). Empty = not attached; the standard `OTEL_EXPORTER_OTLP_ENDPOINT` env var is honored as a fallback, so OTEL can be enabled purely from the environment. |
| `otel.headers` | map | `{}` | Per-request HTTP headers (auth tokens, tenant ids). Values support `${VAR}` env interpolation. |
| `otel.service_name` | string | `reyn` | The `service.name` resource attribute reported to the collector. |
| `otel.capture_content` | bool | `false` | GenAI content-capture gate. `false` (default) emits refs and token/cost counts only — never a raw prompt/response body in a span or log. Set `true` to opt into content capture (only against a trusted collector). |

Requires the OTEL SDK: `pip install reyn[observability]`. An endpoint configured
without the SDK installed logs once and stays not-attached (fail-open) — the
session is unaffected. Full event→span/metric/log mapping, the pinned GenAI
convention version, and the fail-open / recovery-independence guarantees are in
[Reference: observability (OTEL export)](../runtime/observability.md).

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

## `cron` block

Schedule recurring cron jobs. The scheduler runs as part of `reyn web`
(= started in the FastAPI lifespan) or as a foreground process via
`reyn cron run`. Each job declares an `action` (#5209): `message` (default)
dispatches text to an agent's inbox — always starts an LLM turn; `hook`
only fires the `cron_fired` external-event hook — a `hooks.yaml`
`on: cron_fired` entry's own `push_when` then decides whether anything
happens next, at zero LLM turns when it doesn't.

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

    - name: poll_deploy_status   # action: hook — a "token 0" periodic check
      to: ops_agent              # (#5209): no message is delivered here at all;
      action: hook                # the fired cron_fired hook's own push_when
      schedule: "*/5 * * * *"    # decides whether to wake ops_agent
```

### Fields

- **`name`** (required) — job identifier, unique within the schedule
- **`to`** (required, every `action`) — the agent this job runs on (its
  `cron:<job_name>` Session is what `cron_fired` fires on); for
  `action: message` it also doubles as the message RECIPIENT
- **`action`** (optional, default `"message"`) — `"message"`: `to` doubles
  as the recipient and `message` is required, dispatched to its inbox with
  `sender="cron:<name>"` attribution, always starting an LLM turn.
  `"hook"` (#5209): only fires the `cron_fired` external-event hook on the
  host session — never starts a turn itself; `message` must NOT be set (a
  hook job never delivers text). Whether a turn happens next is entirely up
  to a `hooks.yaml` `on: cron_fired` entry's own `push_when` (typically an
  `exec_capture` script) — an unsatisfied `push_when` costs zero LLM turns,
  the reason this action exists (a periodic condition-check that only wakes
  the agent when it finds something).
- **`message`** (required for `action: message`; forbidden for
  `action: hook`) — free-form text delivered to the target agent
- **`schedule`** (required) — 5-field cron expression
  (minute / hour / day-of-month / month / day-of-week)
- **`notify`** (optional) — opt-in unattended notification channel
  (`action: message` only — a hook job's own push, if any, carries its
  own `session`/routing via the hook definition)
- **`input`** (optional, default `{}`) — extra input dict carried on the job
- **`enabled`** (optional, default `true`) — `false` keeps the entry in
  configuration but skips scheduling

> Legacy skill-based jobs (a bare `skill` name) are no longer supported — the
> skill runtime was removed. An old on-disk `cron.yaml` carrying such an entry
> is warned-and-skipped at load, not rejected.

### Cross-references

- `docs/reference/cli/cron.md` — `reyn cron run/list/status`
- `docs/guide/for-users/monitor-and-improve-with-cron.md` — using `cron.jobs`
  together with `.reyn/events/` to wake a monitoring/improvement agent

## `permissions` block

Project-wide capability defaults. Per-skill permissions in `skill.md` override these.

```yaml
permissions:
  exec: deny             # deny | ask | allow — pre-approval key for the `exec` tool
                          # (renamed from `shell` #3226 Phase 3; existing reyn.yaml
                          # `shell:` keys are a clean break, no alias — rename to `exec`)
  file.read:  [".reyn/", "src/stdlib/"]   # flat dotted key — `file:` nested {read:, write:}
  file.write: [".reyn/state/", "reyn/local/"]  # is NOT read; PermissionDecl.from_dict()
                                                # only looks at the flat keys shown here.
  python:                 # ONLY read as a LIST of {function, mode} entries (never a
    - function: compute    # mapping — `{safe: allow}` is silently skipped, 0 bytes
      mode: safe            # read). The sole use: fail load if any entry says
                             # `mode: unsafe` (removed). Grants no runtime authority —
                             # python steps are always sandboxed regardless of this key.
```

MCP server install is gated the same way — via `file.write` (declarative path list, as
above) plus `http.get` (declarative host list) — not a `permissions.mcp_install` bool.
See "MCP install" below for that shape (a blanket `allow`/`deny` scalar, a different
use of the same keys from the list form shown here).

### MCP install

The legacy `permissions.mcp_install: ask | allow | deny` bool axis was removed. MCP install is now gated by the same list axes the rest of the OS uses:

```yaml
# reyn.yaml — install permissions express through file.write + http.get
permissions:
  file.write: allow      # blanket allow for .reyn/config/mcp.yaml (= install target)
  web.fetch: allow       # blanket allow for the registry fetch (= legacy alias)
```

For finer control, the skill's `skill.md` declares the canonical paths and hosts; `startup_guard` prompts the operator once per skill+host, and the runtime check is silent after that (= `file.write` model for paths outside the default zone, `http.get` per-host).

| Want | New shape |
|------|-----------|
| Block all installs project-wide | `file.write: deny` for `.reyn/config/mcp.yaml` paths, or `web.fetch: deny` for the registry host |
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
  file.write: allow     # blanket approval for .reyn/config/mcp.yaml writes
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

The full permission grammar is documented in `reference/config/permissions.md`. Note this section is about *installing* an MCP server — granting an already-installed server's tools to be *called* (`permissions.mcp.<server>: allow`, including `reyn pipe run`'s auto-grant of configured servers) is a separate axis, covered in `reference/config/permissions.md` → "Granting an MCP server permission".

## `${VAR}` interpolation {#var-interpolation}

Any string field in any section of `reyn.yaml` (or `reyn.local.yaml` / `~/.reyn/config.yaml`) can reference an environment variable using `${VAR}` syntax. Variables are resolved from `os.environ` at startup, after `~/.reyn/secrets.env` is loaded into the environment (see [Concepts: secret handling](../../concepts/runtime/secret-handling.md)).

```yaml
# reyn.yaml — ${VAR} works in every string field
llm:
  models:
    default-sonnet:
      model: claude-sonnet-4-5
      api_key: ${ANTHROPIC_API_KEY}          # LLM API key — resolved from secrets.env or shell
      extra_body:
        headers:
          Authorization: ${LITELLM_PROXY_TOKEN}
  api_base: ${LITELLM_API_BASE}            # LiteLLM proxy URL (#4174 T3: nests under llm:, not a separate litellm: key)

mcp:
  servers:
    github:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_PERSONAL_ACCESS_TOKEN}
    internal_tools:
      type: streamable-http
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

## `llm` block

### Proxy / `llm.api_base`

If you route models through a local LiteLLM proxy, put the URL in `reyn.local.yaml` (gitignored), not `reyn.yaml`. You can reference an env var here too:

```yaml
# reyn.local.yaml
llm:
  api_base: ${LITELLM_API_BASE}    # or literal: http://localhost:4000
```

### `llm.prompt_cache_enabled`

When `true` (the default), reyn attaches Anthropic-style `cache_control`
markers to the system prompt so providers that support prompt caching
(Anthropic, AWS Bedrock Claude) can reuse the prefix across calls. Providers
that don't recognize the marker ignore it (Gemini / OpenAI proxies
pass-through).

```yaml
llm:
  prompt_cache_enabled: true
```

## Resolution order

For each setting, reyn merges these sources, lowest priority first — later layers override earlier:

1. **Built-in defaults** — the values shipped with reyn (e.g. `llm.model: standard`).
2. `~/.reyn/config.yaml` — user-global.
3. `reyn.yaml` — project, committed.
4. `reyn.local.yaml` — project, gitignored (machine-local overrides + values written by `reyn config set`).
5. `<project>/.reyn/config/mcp.yaml` — the dynamic MCP server registry. Merged **last for the `mcp.servers` section**, so servers added by `reyn mcp install` override any `mcp.servers` you hand-edit in `reyn.yaml` / `reyn.local.yaml`.
6. `<project>/.reyn/config/cron.yaml` — the dynamic cron registry. Merged **last for the `cron.jobs` section**, so jobs registered at runtime override `cron.jobs` in `reyn.yaml` on a name collision.
7. CLI flags — applied last, per invocation.

Layers 5 and 6 are scoped: each carries only its own section (`mcp.servers` / `cron.jobs`) and is merged section-by-section, so it never touches unrelated settings. `${VAR}` interpolation is applied once after all YAML layers are merged, before CLI flags.

> **Why `.reyn/config/mcp.yaml` and `.reyn/config/cron.yaml` win**: these are the runtime-mutable registries (written by `reyn mcp install` and runtime cron registration) rather than the edit-and-restart static files. Putting them last means a freshly installed server or registered job is the effective entry without the operator also having to touch `reyn.yaml`.

`<project>/.reyn/config.yaml` is no longer loaded — it is a deprecated general-config file, not the active `.reyn/config/mcp.yaml` / `.reyn/config/cron.yaml` registries above. If it still exists on disk, reyn prints a warning and skips it. Move its contents to `reyn.local.yaml`, then delete it.

## `cost` block

Budget caps and rate limits. All fields are optional; omitting a field (or setting its `hard_limit` to `null`) means **unlimited**.

Each token / cost cap (`per_agent_tokens`, `per_agent_cost_usd`, `daily_*`, `monthly_*`) is a `CostLimitConfig` with two sub-fields: `hard_limit` (the cap; `null` = unlimited) and `warn_ratio` (warn threshold as a fraction of `hard_limit`, default `0.8`). Hitting a hard cap refuses the call outright — reyn has no per-dimension ask-for-extension flow today (#4522: an earlier `extension_calls` field promised one, but its only real implementation was a since-removed subsystem, #2448 — the key is now deprecated and ignored if set). The per-dimension `ask_on_exceed` bool was removed earlier (subsumed into `safety.on_limit.mode`, which drives other safety-limit checkpoints — router cap, max hop depth, chain seconds — not the cost caps here).

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

> **Not the same setting as `embedding.cost_warn_threshold`** (below) — the shared
> `cost_warn` substring is a naming coincidence, not a shared concept. This block
> gates a USD/1M-token model **price** threshold, read by the chat router at any
> `/model` switch or session startup, regardless of what the session is doing.
> `embedding.cost_warn_threshold` gates a **chunk-count** threshold read only
> inside the embedding/indexing pipeline before a batch embed call. Different
> unit (USD vs. count), different trigger (model selection vs. indexing), different
> reader (router vs. embedding pipeline) — placement follows *that*, not the name:
> a top-level block for a setting any part of the session can trigger, nested
> under its owning block for a setting only that subsystem's own code path reads.

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

## `offload` block

Opt-in switch for **all three** tool-result size gates (tool-result-schema-redesign §5): the text token cap, the structured-data inline cap, and the media follow-up budget bound. **Off by default** — every tool result is delivered to the LLM in full, never truncated, never offloaded to a file ref. Offloading a large result to a file ref only helps if the model reads the ref back, and mid-tier models often act on the truncated preview instead, degrading the result — so full delivery is the better default. The LLM-visible format (frontmatter + text) is unchanged either way; only whether size gates truncate varies.

Set `enabled: true` to opt in when you want the cost reduction of capping/offloading large tool results. When a single tool result is very large, opting in also keeps it from producing an oversized turn.

The size bounds are tunable too, so "cap, but less aggressively" is expressible — previously the only lever was the boolean and every bound was a fixed constant. Each field below defaults to that former constant, so an existing `enabled: true` config is unchanged, and each falls back independently (setting one does not reset the rest).

```yaml
offload:
  enabled: true              # opt in: cap + offload oversized tool results (default: false)
  max_inline_bytes: 16384    # ceiling on what a capped text result leaves inline
  preview_head_chars: 6000   # how much of the head that inline keeps
  preview_tail_chars: 2000   # …and the tail
  cap_ceil_tokens: 4096      # upper clamp on the per-turn token cap
  cap_alpha: 0.5             # budget-relative term of that cap
  structured_inline_max_chars: 2000  # size at which a dict/list gets its own ref
  structured_preview_chars: 600      # how much of it stays inline beside the ref
```

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | bool | `false` | Master switch. `true` opts in to the text token cap, the structured inline cap, and the media follow-up budget bound. The size fields below apply only while it is `true`. |
| `max_inline_bytes` | int | `16384` | Absolute ceiling on the inline preview a capped text result leaves behind. **Also feeds the turn budget's force-close reserve** — it is what the OS reserves for "one more increment", so lowering it lets a turn run longer before force-close fires. |
| `preview_head_chars` | int | `6000` | How much of the body's head that inline preview keeps. The body itself is stored and referenced, never lost. |
| `preview_tail_chars` | int | `2000` | The same for the tail. |
| `cap_ceil_tokens` | int | `4096` | Upper clamp on the per-turn token cap, so a large-context model still gets a lean inline. |
| `cap_alpha` | float | `0.5` | Budget-relative term: the cap is `min(cap_ceil_tokens, cap_alpha × effective_trigger)`, which keeps a capped turn compactable on a small-context model too. |
| `structured_inline_max_chars` | int | `2000` | Serialized size at which a structured (dict/list) result is stored under its own ref instead of staying inline. |
| `structured_preview_chars` | int | `600` | How much of that serialization stays inline beside the ref. |

## `render_template` block

Output bounds for the `render_template` tool. That tool renders a Jinja2 template against structured data into a string; the sandbox blocks template-injection but not resource exhaustion, so a runaway template (e.g. `{% for i in range(10**9) %}…{% endfor %}`) is capped **during** generation. The render stops the moment either bound is hit and the result is truncated (with a `truncated` flag naming which bound fired) rather than flooding memory or hanging. Raise the bounds for a large report; lower them to harden a shared host.

```yaml
render_template:
  max_output_chars: 256000   # streaming char budget — truncate past this
  wall_clock_seconds: 5.0    # elapsed-time backstop for a runaway loop
```

| Field | Type | Default | Description |
|---|---|---|---|
| `max_output_chars` | int | `256000` | The streaming character budget. The render truncates the moment cumulative output exceeds it. A non-positive or non-numeric value falls back to the default. |
| `wall_clock_seconds` | float | `5.0` | Elapsed-time backstop. Jinja2 exposes no iteration count, so wall-clock bounds a runaway loop that emits little text per step. A non-positive or non-numeric value falls back to the default. |

The defaults are generous enough for real reports / configs and tight enough that a runaway generator stops quickly. Omitting the block leaves both at their defaults (behaviour unchanged).

## `read_cap` block

The per-result inline cap for `file.read` and `load_skill` (#4381 PR-5, architect design). This is a **resource bound**, not a budget bound — a role split the codebase draws deliberately: a resource bound protects a fixed physical resource (memory, transfer size) and is measured in **bytes**, **model-independent**; a budget bound protects the model's context window, is measured in tokens, and is derived from the resolved model (offload's spill trigger, compaction, reactive shrink all live on that side). Before this split, the same cap was scaled by the resolved model's input window and counted in characters — both were defects: window-scaling conflated the two roles, and counting a byte-denominated resource in characters drifted up to ~3× for non-ASCII content.

```yaml
read_cap:
  inline_bytes: 10240   # 10 KiB — ceiling on a single inline read/load result
```

| Field | Type | Default | Description |
|---|---|---|---|
| `inline_bytes` | int | `10240` | Ceiling, in bytes, on what `file.read` / `load_skill` return inline before truncating. A non-positive or non-numeric value falls back to the default. |

**Why 10 KiB**: both consumers gained a resume mechanism the same night this default was chosen — `file.read` via `char_offset` (#4432), `load_skill` via deferring to `read_file(offset=next_offset)` (#4431). A truncated read loses at most one round-trip, not the rest of the content, which is what makes a small default safe rather than arbitrary. Omitting the block keeps this default (behaviour unchanged).

## `history_resident` block

Caps `Session.history`'s in-memory footprint (#4387 Phase B ③, applying #4431's resource/budget role split — see the `read_cap` block above for the full rationale). Same role as `read_cap`: a **resource bound**, measured in **bytes**, **model-independent**. Deliberately a different axis from the on-demand backward-read window (`_HISTORY_HYDRATE_MIN_LINES`, measured in lines) — the two answer different questions ("how much stays resident" vs. "how much is read per on-demand fetch") and are kept as separate knobs on purpose, per #4387's own architect review naming the risk of conflating them.

```yaml
history_resident:
  max_bytes: 268435456   # 256 MiB — ceiling on Session.history's resident size
```

| Field | Type | Default | Description |
|---|---|---|---|
| `max_bytes` | int | `268435456` (256 MiB) | Ceiling, in bytes, on `Session.history`'s in-memory footprint. Once exceeded, the oldest resident entries are evicted (never the just-appended newest one) until the cap is met again. A non-positive or non-numeric value falls back to the default. |

Eviction is not information loss: `Session.history` is a cache, not the source of truth — `history.jsonl` (append-only, on disk) is, and every entry evicted from memory reloads on demand via the already-shipped backward-hydrate path (TUI scrollback paging, in-conversation search, and WAL rewind visibility all already page older entries back in as needed). This closes an unbounded-growth defect (`self.history` previously had no cap at all — see #4387) independent of any claim about what fraction of a given memory ceiling `history` itself accounts for, which this config does not measure or claim to fix.

## `image` block

The fixed row height (in terminal rows/cells) every `present`-rendered inline image is shown at (#4474). Width is derived from this to preserve the image's real aspect ratio — `HalfBlockImage` (reyn's own image renderable, `interfaces/repl/present_renderer.py` — no third-party image-rendering dependency, #4474) takes an explicit width/height in cells with no aspect-ratio derivation of its own; passing a fixed height is what makes aspect-ratio-correct rendering possible at all.

```yaml
image:
  row_height_cells: 20   # fixed height every inline image is rendered at
```

| Field | Type | Default | Description |
|---|---|---|---|
| `row_height_cells` | int | `20` | Fixed height, in terminal rows, for every inline image `present` renders. A non-positive or non-numeric value falls back to the default. |

**Why this is operator-configurable**: the "right" row count is a function of your own terminal height and how much scrollback a photo should occupy — not something reyn can decide for every environment. 20 is a shipped default (tall enough for real photo detail, short enough not to dominate a typical terminal), not a measured "correct" number. Omitting the block keeps this default (behaviour unchanged).

## `tui` block

Operator-tunable inline-TUI presentation thresholds (#4542). Today just the status bar's Telemetry segment (model / agent / cost / context%): the percent at which context-window usage escalates from a bare number (`16%`) to a labelled one (`ctx 82%`).

```yaml
tui:
  context_usage_warn_percent: 80   # ctx% at/above this gets the "ctx" label
```

| Field | Type | Default | Description |
|---|---|---|---|
| `context_usage_warn_percent` | int | `80` | Context-window usage percent at which the status bar labels the figure (`ctx NN%`) instead of showing it bare. A non-numeric value or one outside `0`–`100` falls back to the default. |

**Why this is operator-configurable**: 80 is a shipped default (a plain, unsurprising round number), not a measured "correct" threshold for every operator's own risk tolerance or model/context window — same discipline as `image.row_height_cells` above. Omitting the block keeps this default (behaviour unchanged).

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
      type: streamable-http
      url: https://tools.example.internal/mcp
      headers:
        Authorization: "Bearer ${INTERNAL_TOOLS_TOKEN}"
```

| Field | Type | Required for | Description |
|-------|------|--------------|-------------|
| `type` | string | all | `stdio` \| `streamable-http` \| `sse` |
| `command` | string | stdio | Executable to spawn. |
| `args` | list[string] | stdio (optional) | Argument vector passed to `command`. |
| `env` | map[string,string] | stdio (optional) | Extra environment variables for the spawned process. Values support `${VAR}` expansion. |
| `network` | bool | stdio (optional) | Whether the sandboxed server may use the network. Defaults to the same single-source default as `sandboxed_exec`. Set `false` to isolate a server that should never reach the network. Operator-owned — the model cannot set it. |
| `subprocess` | bool | stdio (optional) | Whether the sandboxed server may spawn child processes (fork). Defaults to `true` — most stdio servers launch via a fork-based launcher (`npx` → node, `uvx` → the tool) and must fork to start. Set `false` to harden a genuinely fork-free server. Operator-owned — the model cannot set it. |
| `write_paths` | list[string] | stdio (optional) | **Filesystem paths the sandboxed server may write**, in addition to its working directory (which is always granted). `~` is expanded. Operator-owned — the model cannot set it. A launcher bootstraps into a per-user cache outside the workspace, so reyn grants a **default** scope for the launchers it recognises (`npx`/`npm` → `~/.npm`; `uvx`/`uv` → `~/.cache/uv` + `~/.local/share/uv`). Set this when your server's runtime is not one of those, or when you have relocated its cache (e.g. `XDG_CACHE_HOME`, `npm_config_cache`) — the defaults are keyed to the standard locations and cannot know yours. Declaring `write_paths` **replaces** the built-in defaults, so you can narrow as well as widen. When a write is denied, the error names the denied path and points back at this knob — on BOTH the paths a denial can take: a server that fails to *start* with `Operation not permitted` / `EPERM` (its launcher could not reach its own cache), and a *tool call* refused a write to a path you passed it (the server started fine; the path is simply outside its scope). Grant the named path here — or, for the tool-call case, pass a path inside the server's working directory instead, which needs no declaration at all. **Keep the scope tight**: a write grant also re-opens *reads* for that path. Granting `~` does not defeat the sensitive-read deny-list (`~/.ssh`, `~/.aws`, …) — the deny wins over an overlapping write grant (#2978) — but it needlessly widens the surface, so grant the specific cache directory, never the home directory. |
| `url` | string | http, sse | Endpoint URL. |
| `headers` | map[string,string] | http, sse (optional) | Static request headers. Values support `${VAR}` expansion. |
| `call_timeout_seconds` | float | all (optional) | **End-to-end bound on every MCP op** (list / call / probe), default `120`; `<= 0` opts out. Covers connecting the server *and* running the op, so it also bounds a launch — see `init_timeout` for why that matters. Set it when a server is known to be slow (raise) or known to be quick and you want fail-fast (lower). Per-call, it is passed to the MCP SDK's `read_timeout_seconds`, overriding the session-level default that `timeout` sets for `type: streamable-http`. |
| `init_timeout` | float | all (optional) | **How long to wait for the server to complete the MCP handshake**, default `60`; `0` disables this bound. Bounds only the handshake, never a tool call — so raising it cannot make a slow tool time out. A server that starts but never speaks (the classic case: `command: uvx <pkg>`, which downloads from PyPI on first run and stays silent until it finishes) stalls here; on expiry reyn fails with an error naming the likely cause and the remedy. The default sits below `call_timeout_seconds`' `120` deliberately: both bounds cover the launch, and whichever fires first decides what you get to read — the generic per-op timeout cannot tell you *why* a launch stalled. So if a genuinely slow server needs more room, **raise both** — `init_timeout` alone cannot buy more time than `call_timeout_seconds` allows. The durable fix for a slow first run is to pre-install the package and point `command` at the installed executable, which is also what lets the server start offline or behind a proxy. |
| `elicitation` | string | all (optional) | `prompt` (default) — a server-initiated structured-input request (`elicitation/create`) surfaces as a consent prompt; `auto_decline` — every such request is declined without prompting. See [Concepts: MCP § Elicitation](../../concepts/tools-integrations/mcp.md#elicitation-structured-input-requests-from-a-server). |
| `elicitation_timeout_seconds` | float | all (optional) | Wall-clock deadline for a human to answer an elicitation prompt. Default `120`. An unanswered request past the deadline is cancelled. |

`${VAR}` in any string value is expanded from `os.environ` at startup (after `~/.reyn/secrets.env` is loaded). Missing variables expand to `""` and emit a runtime warning. Use `reyn secret set` to store values in `~/.reyn/secrets.env` — never paste tokens into `reyn.yaml` directly.

Servers are merged across config sources: `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml`. The merge is a shallow union on `mcp.servers` keys — a per-machine `reyn.local.yaml` can add or override a single server without re-stating the rest.

The MCP runtime ships in the core install — every session's MCP client is built directly on the official `mcp` SDK (#4283/#4298/#4299: `fastmcp` retired from the client path entirely), which is a core dependency, so no extra is required. `fastmcp` is DROPPED from every reyn-side dependency declaration — core AND every extra (#4302: `tests/_support/`'s MCP server test-doubles were ported onto `mcp`'s own bundled `mcp.server.fastmcp` server framework). This is not the whole footprint, though: the bundled `rag` plugin declares its OWN `fastmcp>=3.4,<4` in its own `requirements.txt` (register-only, never read or installed by reyn's own `plugin_install`) — a real consumer #4302 found; the upper bound was tightened in #4388 after a loose `>=2.0` floor (permitting an untested major version) turned out to be a red herring in a #4371 CI investigation, not the actual cause, but a real latent risk on its own. reyn's dev/CI tooling that runs the plugin's scripts directly (a sandbox gate spawning them as real subprocesses, and a wheel-reachability smoke test's own MCP client) installs that `requirements.txt` as its own setup step, not by reintroducing fastmcp to any `pyproject.toml` declaration. `mcp` is pinned `>=2.0,<3.0` (#4412: bumped off the prior `>=1.24,<2.0` floor once reyn's own MCP *server* — `src/reyn/mcp/server.py`, which had depended on the `lowlevel.Server` decorator API mcp 2.0 removes — was ported onto 2.0's constructor-kwarg registration shape via the `_mcp_server_boundary.build_mcp_server` seam, #4368). A now-empty `[mcp]` extra is retained as a back-compat alias so existing `pip install -e ".[mcp]"` invocations keep resolving.

See [Concepts: MCP](../../concepts/tools-integrations/mcp.md) for the protocol overview and How-to: use an MCP server for the end-to-end quickstart.

> **`mcp.search_threshold` removed (#3218 / FP-0066 §7 P1a).** The
> `ReynConfig.mcp_search_threshold` field + its `mcp.search_threshold:`
> parsing were fold-removed as a confirmed no-op: the parsed value was never
> threaded through to `build_tools()` by either router_loop.py call site, so
> setting it never took effect. `build_tools()`'s own `mcp_search_threshold`
> parameter still exists (default `0` — always inline; see
> `MCP_SEARCH_THRESHOLD` in `src/reyn/runtime/router_tools.py`), but it is no
> longer reachable from `reyn.yaml` — a caller wanting non-default behavior
> passes it explicitly in code. Full removal of the underlying
> `tool_search_tool` mechanism is tracked as FP-0033.

## `skills` block

Registers `SKILL.md`-based skills — the same explicit-registration model as `mcp.servers` (no directory scan; an entry must exist for a skill to be visible).

```yaml
skills:
  entries:
    pdf_editing:
      path: skills/pdf-editing/SKILL.md   # project-root-relative or absolute
      description: "Fill, merge, and extract fields from PDF forms"
      enabled: true
      visibility: menu                    # menu | on_demand | hidden
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `path` | string | required | Path to `SKILL.md`, or its containing directory. |
| `description` | string | `""` | One-line summary shown in the model-facing `## Skills` menu (first line only, 1024-char cap — the [Agent Skills specification](https://agentskills.io/specification)'s maximum for `description`; #3550). |
| `enabled` | bool | `true` | `false` removes the entry from the registry entirely. Dominates `visibility`. |
| `visibility` | enum | `menu` | Which surface names the skill: `menu` (in the `## Skills` system-prompt menu) \| `on_demand` (not in the menu, but returned by the `skill_list` tool — no standing token cost) \| `hidden` (no model-facing surface at all). |

`enabled: false` drops the entry before `visibility` is consulted, so the pair spans 4 states, not 6.

**Removed in #2971: `auto_invoke`** (a misnomer — nothing auto-invokes a skill; it only controlled menu rendering, which was then the sole surface naming a skill, so `false` made the skill unreachable rather than merely unadvertised). A config still carrying it fails at load naming the replacement: `auto_invoke: true` → `visibility: menu`; `auto_invoke: false` → `visibility: hidden`.

`skills.entries` merges across `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml` ⊕ the dynamic `<project>/.reyn/config/skills.yaml` (written by the `skill_install_local` / `skill_install_source` chat tools), later tiers winning on name collision — the same merge shape as `mcp.servers`.

See [Concepts: Skills](../../concepts/tools-integrations/skills.md) for the full registration model, the three-layer exposure model (menu / on-demand read / bundled assets), and the install tools.

## `pipelines` block

Registers pipeline DSL files — the same explicit-registration model as `skills.entries` / `mcp.servers` (clean break: there is no directory scan; a `*.yaml` file with no config entry is invisible to every session).

```yaml
pipelines:
  entries:
    greetings:                       # entry KEY = the namespace label
      path: pipelines/hello.yaml   # project-root-relative or absolute
      description: "Minimal greeting pipeline"   # optional
      enabled: true                              # optional, default true
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `path` | string | required | Path to the pipeline's `*.yaml` DSL file (may hold multiple `---`-separated `pipeline:` documents). |
| `description` | string | `""` | Optional one-line summary; if omitted, the DSL's own `description:` key is used. |
| `enabled` | bool | `true` | `false` removes the entry from the registry entirely. |

The entry **key is a pure namespace label** — it need not equal any declared `pipeline:` name. Every pipeline in the file registers under the global name `{key}.{declared-name}` (namespacing is always on). A `.` is reserved as the namespace separator, so it is forbidden in both an entry key and a declared `pipeline:` name. A dot-less `call`/`match` target resolves to a same-file sibling (`{key}.name`); a dotted target is a global reference (`other_key.name`).

`pipelines.entries` merges across `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml` ⊕ the dynamic `<project>/.reyn/config/pipelines.yaml` (written by the `pipeline_install_local` / `pipeline_install_source` chat tools), later tiers winning on name collision — the same merge shape as `skills.entries` / `mcp.servers`.

See [Concepts: Pipeline registration](../../concepts/runtime/pipeline-registration.md) for the full registration model and the install tools.

## `presentations` block

Registers **named presentation templates** for the `present` op — the same explicit-registration model as `skills.entries` / `pipelines.entries` / `mcp.servers`. A named template's value is a **blueprint**: the identical declarative, non-executable component tree an inline `present` blueprint is (catalog components + JSON-Pointer path bindings). The blueprint lives **inline** in the entry (no file indirection — a blueprint is small declarative data), and is structurally validated at load time.

Registering a named template is an **operator/config action** — there is no install tool and no op the model can call to register one. The model authors *inline* blueprints only; a named `template:` in a `present` op is a read-only lookup against this registry. An unknown template name is not an error: the `present` op falls back through a content-type default viewer to a generic YAML/text view, so the data always reaches the user.

```yaml
presentations:
  entries:
    search_results:
      blueprint:                              # required; inline component tree
        - component: table
          rows: {"$bind": "/results"}
          columns:
            - {header: Author, path: /author}
            - {header: Title,  path: /title}
      description: "Search results table"      # optional
      enabled: true                            # optional, default true
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `blueprint` | list or object | required | The declarative component tree (same shape + catalog as an inline `present` blueprint). Validated at load; a malformed blueprint is skipped (logged), or on hot-reload rejects the whole reload (last-good kept). |
| `description` | string | `""` | Optional one-line summary. |
| `enabled` | bool | `true` | `false` removes the entry from the registry entirely. |

`presentations.entries` merges across `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml` ⊕ the dynamic `<project>/.reyn/config/presentations.yaml`, later tiers winning on name collision — the same merge shape as `skills.entries` / `pipelines.entries` / `mcp.servers`. The `<project>/.reyn/config/presentations.yaml` layer hot-reloads at the turn boundary, so a newly-registered template becomes resolvable on the next turn without a restart.

## `embedding` block

RAG embedding model classes and batch settings.

`enabled` is the one switch and it is **off by default** — nothing else in this
block turns embedding on. Once it is on, the built-in defaults cover the OpenAI
path, so a fresh install with `OPENAI_API_KEY` needs no further changes. See
[Guide: enable semantic search](../../guide/for-users/enable-semantic-search.md)
for the full opt-in walkthrough, including offline/air-gapped guidance.

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
> `embedding.enabled: false` to opt out. reyn surfaces this exact
> guidance when the index build fails with an `UnsupportedParamsError`.

```yaml
embedding:
  enabled: false                  # default (off, opt-in) — provider/cost gate only (#4156)
  index:
    actions: true                 # default — the ~10-entry action/mcp/pipeline catalog
    repo_knowledge: false         # default — the repo-wide knowledge index (#4156)
  default_class: standard         # class to use when no class is specified
  batch_size: 100                 # texts per embedding API call (1–2048)
  max_concurrent_batches: 1       # parallel batch calls in flight (1–10)
  max_retries: 3                  # transient-error retries (0–10)
  retry_backoff: exponential      # exponential | linear
  timeout: 60.0                   # per-attempt deadline, seconds (<= 0 opts out)
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
| `enabled` | bool | `false` | **The provider/cost gate — one meaning only** (#4156): may reyn call an embedding provider at all. Default `false` (opt-in / predictable-safe default — embedding needs a provider + cost). Clean-break replacement for the retired `action_retrieval.embedding_class` gate (no alias) — the on/off decision lives here; the model class is the separate `default_class` field below, unaffected. **Symmetric model**: `enabled: false` hides everything below regardless of `index.*` — non-semantic discovery (`list_actions`, …) and load/invoke verbs (`invoke_action`, …) are unaffected. When `embedding.enabled` is `false`, the `embed` op pre-flights and returns a decision-enabling `status: "blocked"` result (naming this key) rather than a silent no-op or an opaque provider error. Prior to #4156, this single flag ALSO decided WHAT gets embedded (bundling the action catalog with an unconditional repo-wide index build, every router-loop turn, with no way to get one without the other) — that decision now lives in `index` below. |
| `index.actions` | bool | `true` | #4156 — build the ~10-entry action/mcp/pipeline catalog index `search_actions` depends on, when `enabled: true`. Negligible TPM contribution (fixed, small population) — kept on by default so the pre-#4156 `search_actions` experience is unchanged for an operator who never touches this field. |
| `index.repo_knowledge` | bool | `false` | #4156 — build the FP-0066 P3b **repo-knowledge index** (`knowledge_repo_doc` + `knowledge_repo_src` — every reachable `.md` doc and every other source file in the repo, chunked — measured at ~1,609 chunks / ~4.86M tokens on this repo at #4156's filing, and it scales with the repo, not fixed), when `enabled: true`. Scheduled on **every router-loop turn** (`sync_repo_ingest_background`, a no-op once the index is clean). **Default `false`** — this is the workload that burned through an owner's 5M TPM budget in one burst while they only wanted the ~10-entry action catalog (TPM is a tokens-per-minute ceiling; batching cannot reduce total tokens sent, only not indexing can). When this field is `false` and a turn would have ingested the repo-knowledge index, reyn logs one line per process (`repo knowledge indexing is off (embedding.index.repo_knowledge: false, the default) — ...`) rather than skipping silently. |
| `default_class` | string | `standard` | Class used when embedding ops don't specify one (used only when `enabled: true`). Must be a key in `classes`. |
| `batch_size` | int | `100` | Texts per embedding API call. Valid range: 1–2048. |
| `max_concurrent_batches` | int | `1` | Parallel batch calls in flight. Valid range: 1–10. Values > 1 are accepted but log a warning until concurrent support lands. |
| `max_retries` | int | `3` | Transient-error retries per batch call. Valid range: 0–10. |
| `retry_backoff` | string | `exponential` | Backoff strategy: `exponential` or `linear`. |
| `timeout` | float | `60.0` | Per-attempt deadline in seconds — how long reyn waits for one embedding attempt before giving up. `<= 0` opts out (no bound — the call is then capped only by litellm's own `request_timeout`, 6000s/attempt, which is indistinguishable from a hang; a warning is logged). The default matches `safety.timeout.llm_call_seconds`: an embedding call is the same kind of call as a chat LLM call. Applies **per attempt**, so the worst-case **wait** is `timeout × max_retries` plus backoff. **This bounds waiting, not spending — see the note below.** |

> **`embedding.timeout` does not reduce what you are billed for.** It caps how long reyn *waits*; it does not cap how many requests the provider *receives*. One attempt can put up to **3** HTTP requests on the wire — the OpenAI SDK client retries internally (`max_retries=2` by default), underneath litellm and underneath this knob — so `max_retries: 3` can deliver up to **9** requests for a single `embed`. Measured against a fast-erroring provider, all 9 are delivered in ~7.6s with the default `timeout: 60.0`: the bound never engages at all. **Lowering `timeout` does not lower that count**, and reyn's own retry log (`attempt 1/3`) counts attempts, not requests. reyn's embedding cost report records at most one response per `embed`, so it is a lower bound on requests delivered. See [#3047](https://github.com/tya5/reyn/issues/3047) for the measurements and the open decisions.
| `tokenizer` | string | `cl100k_base` | tiktoken encoding used for chunk-size estimation. |
| `cost_warn_threshold` | int | `10000` | Estimated chunk count above which the `ask_user` gate fires before indexing. Unrelated to the top-level [`cost_warn` block](#cost_warn-block) — see the note there on why they're separate despite the shared name. |

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

All three built-in classes are OpenAI-backed via litellm. There is no in-process local backend (#3128 removed the `local-mini` / `local-e5` sentence-transformers classes and the `reyn[local-embed]` extras) — an operator who wants a local/offline model adds a custom `embedding.classes` entry naming a model served behind an operator-run litellm proxy. See [Concepts: RAG — Local and offline embedding models](../../concepts/data-retrieval/rag.md#local-and-offline-embedding-models) for the setup.

## `chat` block {#chat-compaction-block}

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
    max_schema_reprompt_attempts: 1    # bounded re-prompt budget on invalid compaction JSON
    recovery_policy: next_turn         # #5296: compaction only; spill follows the constraint
    max_shrink_iterations: 8           # retry_loop's overflow-recovery safety cap (escape valve, not a cure)
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
| `max_schema_reprompt_attempts` | int | `1` | #4883: bounded re-prompt budget when the compaction LLM's JSON response has an empty/missing `topic_arc` (whose emptiness can't be told apart from a dead response). Exhausting the budget raises rather than silently accepting an empty summary. `0` = raise on the first invalid response, no re-prompt. `new_turn_seqs` plays no part in this: `covers_through_seq` is derived from `compact()`'s own input, never read from an LLM echo (#4951-A) — and #4951-B removed the `new_turn_seqs` key from the schema/prompt entirely, so there is nothing left to gate or not gate. |
| `recovery_policy` | `never` \| `next_turn` | `next_turn` | #5296: controls only irreversible compaction after measured byte-limit exhaustion. `next_turn` preserves the current behavior: compaction is persisted for the following turn; `never` skips compaction and ends with the existing structured error. It does not control spill; spill is triggered by the constraint itself. |
| `max_shrink_iterations` | int | `8` | #4957: `retry_loop`'s own safety cap on overflow-recovery iterations. **This is an escape valve, not a cure** — raising it only delays exhaustion if the underlying cause (a persistent HTTP 413, a 5xx, a rate limit) never resolves on its own; it does not fix that cause. Must be `>= 1` (`0` would never run the shrink loop at all, raising immediately on the first overflow). Distinct from `RouterLoop`'s own unrelated `max_iterations` (the tool-call loop bound) — do not confuse the two. |
| `use_chars4_estimate` | bool | `false` | When `true`, use `len(text)//4` for token estimation instead of `litellm.token_counter` (latency opt-out for large deployments). |

### `chat.compaction.section_token_caps` fields

**Only `topic_arc` is an enforced cap.** All five values are serialized into
the compactor's prompt as size *guidance* for the LLM, but `decisions` /
`pending` / `session_user_facts` / `artifacts_referenced` are taken directly
from the LLM's parsed response with no post-processing — nothing truncates
them if the model overshoots. `topic_arc` alone goes through a 3-tier
deterministic bound after the LLM returns (fit → LLM re-summarize, bounded by
`resummarize_passes` → a deterministic `hard_truncate_summary` floor, #1163),
so `topic_arc` never exceeds its body budget; the other four can.

This is a real, current asymmetry, not a design intentionally scoped that
way — no record motivating `topic_arc`-only enforcement was found; #1163
replaced `topic_arc`'s previous blind character-cut, and the other four
never had any bound of their own to begin with. It is not being closed:
an oversized `decisions`/`pending`/etc. section survives at most one turn.
`router_loop_driver.py`'s pre-frame guard (`context_budget_advisor.
maybe_force_compact`) recomputes the effective token budget before every
send and forces another compaction pass if the current history still
exceeds it — so an overshoot from one of the four un-enforced sections is
caught and re-compacted at the very next turn, at the cost of that one
turn running with a larger-than-configured section rather than a hard
failure or an unbounded blow-up.

| Field | Default | Description |
|-------|---------|-------------|
| `topic_arc` | `200` | Token target for the topic-arc summary section — the only one of the five that's enforced after the LLM returns (see above). |
| `decisions` | `400` | Token target for the decisions section, given to the LLM as prompt guidance — not enforced on the returned value. |
| `pending` | `400` | Token target for the pending-items section, given to the LLM as prompt guidance — not enforced on the returned value. |
| `session_user_facts` | `200` | Token target for user-facts carried across compactions, given to the LLM as prompt guidance — not enforced on the returned value. |
| `artifacts_referenced` | `300` | Token target for artifact reference listings, given to the LLM as prompt guidance — not enforced on the returned value. |

### Removed keys

`head_size`, `tail_size`, `trigger_total_tokens`, and `min_compact_batch` are
no longer recognised. If present in your `reyn.yaml`, Reyn emits a
`DeprecationWarning` at startup and ignores them. Remove these keys — head/tail
sizing is now token-budget via `component_weights`, and auto-compaction is
window-relative.

## `audit_events` block

Audit-log rotation + automatic purge policy for chat-session event files. Skill-run events use one file per run and are not affected by this setting.

Renamed from `events:` (#4174 T5) — bare "event" is ambiguous in reyn (an
"event" is one of audit-event / WAL-event / hook-event); this block was
always audit-event rotation only.

```yaml
audit_events:
  max_bytes: 10485760              # rotate at 10 MB (default)
  max_age_seconds: 86400           # rotate after 1 day (default)
  cleanup_period_days: 30          # auto-delete files older than 30 days (default)
  max_disk_usage_percent: 10       # auto-delete oldest files past 10% of free space (default)
  backend: local                   # where events are written (default)
  agent_delta_coalesce_fragments: 100    # durable write every N streamed chunks (default)
  agent_delta_coalesce_interval_ms: 2000 # or every T ms, whichever first (default)
  agent_delta_include_text: false        # keep the reply's own content in the durable record? (default)
  completed_response_include_text: false # keep the completed reply / ask_user question in the durable record? (default)
  user_input_include_text: false         # keep the user's own words in the durable record? (default)
  provider_body_include_text: false      # keep a provider's own error-response body/text? (default, lattice-meet)
  provider_body_max_chars: 4000          # cap on the kept provider_body/provider_response (default)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_bytes` | int | `10485760` (10 MB) | Rotate the active event file when it exceeds this size. `0` = no size-based rotation. |
| `max_age_seconds` | int | `86400` (1 day) | Rotate the active event file when it exceeds this age in seconds. `0` = no age-based rotation. |
| `cleanup_period_days` | int | `30` | Automatic-purge age axis (#4479) — files whose filename date is older than this many days are deleted. `0` disables this axis. |
| `max_disk_usage_percent` | float | `10` | Automatic-purge size axis (#4479) — once the events directory's own total size exceeds this percent of the filesystem's current FREE space, oldest files are deleted until back under. `0` disables this axis. |
| `backend` | string | `local` | **#4496 — where audit-events are WRITTEN.** `local` (default) preserves current behavior — events land under `.reyn/events` exactly as before this field existed. `discard` (sink-null) writes nothing to disk; subscriber delivery (the TUI/AG-UI forwarders, hooks, OTEL) and the per-emitter `audit_seq` continuity (#4496 PR-1) are UNCHANGED either way — see [Concepts: events](../../concepts/runtime/events.md#write-side-backend-4496) for the structural guarantee. **`discard` means `reyn events replay` / support-bundle / dogfood_trace have nothing to read for this run — in particular, support-bundle is the tool operators use to report bugs, so this trades that away.** `network` is not yet a valid value (an unrecognized string, `network` included, falls back to `local`) — its on-failure semantics are an open design question tracked in #4496. |
| `agent_delta_coalesce_fragments` | int | `100` | **#4960 — architect ruling C.** `agent_delta` is durably written once per this many RAW PROVIDER CHUNKS (or `agent_delta_coalesce_interval_ms`, whichever comes first), per streaming chain, plus one final record when a stream ends. **#5261/#5268: an `agent_delta` event may itself already merge more than one raw provider chunk** (source-side merge at the LLM-call boundary, before this event is ever emitted) — the threshold's UNIT was kept as raw chunks (it sums each event's own `raw_chunk_count`, not a flat `+1` per event), so the default number of durable records per streamed reply is UNCHANGED by that merge; only the write side's own counting arithmetic became correct again. Live TUI/AG-UI delivery is UNAFFECTED (every emitted event still dispatches to subscribers unthrottled) — this only throttles what reaches `.reyn/events`. Measured (2000-delta/60KB real streamed reply, `agent_delta_include_text=true`, predating #5261's source-side merge — one event per raw chunk at measurement time): unthrottled, `agent_delta` was 99.4% of that run's audit file bytes — this figure assumes `text` is being written; with `agent_delta_include_text`'s default (`false`, below), a coalesced record's bytes are smaller and this percentage does not apply as measured. A non-positive or non-numeric value falls back to the default. |
| `agent_delta_coalesce_interval_ms` | int | `2000` | **#4960 — the same coalescing window's time axis**, in milliseconds. Primarily protects against a process-level death (SIGKILL / OOM-kill / host crash) that the terminal-flush-on-stream-end mechanism cannot catch (a Python `finally` never runs then) — secondarily gives periodic evidence for an idle-but-long-lived stream. A non-positive or non-numeric value falls back to the default. |
| `agent_delta_include_text` | bool | `false` | **#4666 item ① — opt-in for the streamed reply's own CONTENT** in the durable `agent_delta` record (mirrors the OpenTelemetry GenAI convention: "every attribute that can hold prompt/output content is opt-in, default metadata-only"). ITS OWN knob, deliberately not tied to `agent_delta_coalesce_*` above (owner ruling: each content opt-in gets a separate toggle) — coalescing still happens regardless of this flag; only the `text` field WITHIN an already-coalesced record is conditional. Off (default): the durable record keeps `chain_id`/`round_index`/`coalesced_fragment_count`/`audit_seq` but drops `text` — #4960's own reason for coalescing ("a partial reply of N fragments existed", for cost accountability) still holds without it. **⚠️ Default-behavior change, 2026-08-21 (#4666, owner ruling): before this field existed, `agent_delta`'s reply content was ALWAYS durably recorded — no opt-in or opt-out existed. If you relied on `.reyn/events` carrying streamed-reply text, set this to `true`.** UNCHANGED either way: live TUI/AG-UI delivery (every fragment, full text, always) and `history.jsonl` (the completed reply's own persistence, a separate mechanism entirely). |
| `completed_response_include_text` | bool | `false` | **#4666 item ② — opt-in for the completed model→user text**: `agent_response_committed` (new kind, [events reference](../runtime/events.md) — the terminal reply, force-close/wrap-up text, tool_calls-round accompanying text) and `user_intervention_requested`'s `question`/`suggestions`/`options` (the model's `ask_user` question — governed by ② because it is content the MODEL directed at the user, the SAME reason every other kind this field covers is ②'s, not because it must share a knob with anything else). ITS OWN knob, separate from `agent_delta_include_text` above AND `user_input_include_text` below (owner ruling, same instruction: one toggle must never cover more than one content opt-in) — turning ON only one of ② or ③ for an `ask_user` exchange is a valid, deliberate operator choice (e.g. keep the question, drop the answer), not a defect. Both events fire unconditionally; off (default), `LocalEventBackend.write()` drops only the free-text field(s) — every other field (`chain_id`/`intervention_id`) is kept, so "a response was committed"/"a question was asked" remains provable without content. UNCHANGED either way: live TUI/AG-UI delivery and any opt-in OTEL subscriber. **This field also governs `ask_user`'s `question` on `tool_called.args`** (#4666 item ③b, `reyn.core.dispatch.content_declarations` — the dispatcher-level gap #4970's review found; see `user_input_include_text`'s row for the matching `answer` half and its own remaining scope note). |
| `user_input_include_text` | bool | `false` | **#4666 item ③ — opt-in for the user's OWN typed/chosen text**, ITS OWN knob (separate from `agent_delta_include_text` and `completed_response_include_text` above, both tracked under #4666). Covers 6 kinds, one content field each (AST census — an earlier pass found 3 and undercounted): `user_submitted`/`user_message_received` (`text`), `intervention_answer_submitted` (`text`), `user_answered_intervention` (`answer_text`), `user_intervention_received` (`answer`), `router_retry_exhausted` (`user_message`, truncated to 200 chars at the emit site regardless of this flag). No coalescing here — each event is still written individually; the knob only decides whether the one content field survives. Off (default): every other field on the kind (`chain_id`/`intervention_id`/`msg_id`/`seq`/etc.) still records that a submission/answer happened. **⚠️ Default-behavior change, 2026-08-21 (#4666): before this field existed, these 6 kinds' content was ALWAYS durably recorded.** UNCHANGED either way: live subscriber delivery (TUI/AG-UI/peer broadcast) and `history.jsonl`. **This field also governs `ask_user`'s `answer` on `tool_returned.result`** (#4666 item ③b: a per-tool "this field is conversation content" declaration, `reyn.core.dispatch.content_declarations` — closes the gap #4970's review found, where `ask_user`'s question/answer reached the audit log via the generic `tool_called`/`tool_returned` kinds unconditionally, bypassing ②③ entirely). **Scope, measured, not extended by guess:** only `ask_user` declares any field today (the sole tool whose args/result structurally ARE a conversation exchange); `mcp_called.args` was measured and excluded — an MCP tool's args are model-decided call parameters, the same class as any other tool's args, not a dedicated question/answer channel. A tool that shows the user free text and forgets to declare it leaks that text silently — this bound only catches the declared set GROWING, never catches a tool that should have declared and didn't (see the registry module's own docstring for the full disclosure). |
| `provider_body_include_text` | bool | `false` | **#4975 — architect ruling (c), a LATTICE-MEET, not its own independent opt-in.** Gates `llm_request_error`'s `provider_body`/`provider_response` (a provider's own error-response body — reyn does not control its shape, so a 4xx/5xx body could quote back request content of any of the 3 #4666 content classes above; reyn cannot tell in advance which one). Showing either field requires ALL of `agent_delta_include_text` AND `completed_response_include_text` AND `user_input_include_text` above AND this field's own opt-in — the narrowest participant wins (same `compose_resolved` lattice-meet idiom this repo's permission resolution already uses). Rejected alternatives: OR-composition (any one opt-in lets all 3 content classes through — too loose) and a brand-new independent knob (doesn't correspond to a payload an operator can name). `error_type`/`status_code` are always recorded regardless of the gate; `provider_body_length`/`provider_response_length` are also always recorded when a body existed, gate-independent, so "existed but hidden" stays distinguishable from "genuinely absent". `LocalEventBackend.declare_gaps()` names this gap while any of the 4 participants is off. Whether providers actually echo conversation content in their error bodies is a separate, unmeasured question tracked in #4975's own issue — this knob only builds the permission surface for when that's confirmed. |
| `provider_body_max_chars` | int | `4000` | **#4975 — the cap on the SHOWN `provider_body`/`provider_response`** when `provider_body_include_text`'s meet holds (reyn cannot bound a provider's own body size otherwise). `provider_body_truncated`/`provider_response_truncated` is added only when the cap actually cut something, so a caller can tell a capped body from a genuinely short one. A non-positive or non-numeric value falls back to the default. |

Setting both `max_bytes` and `max_age_seconds` to `0` disables rotation entirely. `backend: discard` makes both rotation fields moot (nothing is ever written to rotate).

### Automatic purge (#4479)

Either axis firing deletes files — `cleanup_period_days` OR `max_disk_usage_percent`, whichever is touched first (not `and`: disabling one axis must not silently disable the other). Runs fire-and-forget, off the event loop, at session start and at every rotation; `reyn events purge --before <DATE>` remains available for a manual, explicit run and shares the same file-selection code.

Both defaults are **borrowed conventions, not measurements** of reyn's own `.reyn/events` growth rate (unmeasured as of #4479): 30 days borrows the nearest comparable local-agent CLI's own default (Claude Code's `cleanupPeriodDays`); 10% borrows systemd-journald's own `SystemMaxUse` convention. Neither is "the correct" number — both exist as an operator-overridable starting point.

**`0` means disabled on that axis, not rejected** — a deliberate choice, documented here on purpose: Claude Code carries an open report of its own `cleanupPeriodDays` knob being ambiguous about whether `0` means "delete immediately" or "never delete." reyn's own knobs use `0` = never delete, unambiguously, on both axes.

## `artifacts` block

The artifact-ref table fallback's own row cap (#4601). A remote client's — and a local client right after a restart's, #4584's own measured finding — Artifacts pane consults the durable, append-only, persist-tier artifact-ref table when its live conversation view carries nothing. With no cap, this read is unbounded and only ever grows.

```yaml
artifacts:
  remote_fallback_limit: 50   # default
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `remote_fallback_limit` | int | `50` | Caps the ref-table fallback to the N NEWEST entries (newest-first). A non-positive or non-numeric value falls back to the default. |

`remote_fallback_limit` is a **UX-scale default, not a performance one** — a single `stat()` costs order-microseconds, so even 10,000 rows costs tens of milliseconds; the binding constraint is how many newest-first rows an operator would ever actually scroll through in a list pane, which is a couple of dozen at most. The Artifacts pane's own disclosure text always states "newest N of M" so a truncation is never silent — raise this value if your own usage wants more history visible at once.

## `storage` block

The PROJECT-wide (cross-session) disk-usage cap on `.reyn/memory/history-content/` — the offloaded tool-result content every session in this project writes (#5366). Unlimited (off) by default: with no `max_bytes`, the cap never fires.

```yaml
storage:
  max_bytes: 10737418240   # e.g. 10 GiB; omit for unlimited (default)
  pin: []                  # agent names whose content is never evicted
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_bytes` | int \| null | `null` (unlimited) | Project-wide cap across every session's history-content. A non-positive or non-numeric value falls back to unlimited — the same state as omitting the key entirely, never a silently-active cap of some other size. |
| `pin` | list[str] | `[]` | Agent names (not session ids) whose own history-content is NEVER an eviction candidate under this cap, regardless of whether that agent's process is currently running. A non-list value, or a non-string list entry, is dropped rather than propagated. |

This is a **DIFFERENT number from `MediaStoreConfig`'s own per-store `history_content_max_bytes`** (2 GiB, not operator-configurable here) — that field is a per-SESSION fail-safe backstop; this one bounds the WHOLE `.reyn/memory/history-content/` tree across every session in the project, which is the number an operator can actually name (nobody chooses how many sessions a project spawns, so "N bytes per session" would silently mean "N × session-count total"). The two never share a name on purpose — reusing one name for both would make it unreadable which cap is actually in effect for a given eviction.

## `voice` block

Voice-input (Whisper) settings for the inline CUI's F2 dictation binding — revived (#4187/#4249) as a reimplementation against the current CUI, after the original Ctrl+R binding was deleted along with the retired Textual TUI it was built for. See [concepts: voice](../../concepts/tools-integrations/voice.md). Optional — requires `pip install 'reyn[voice]'` (`sounddevice` + `faster-whisper`). The block is lazy-loaded; a missing `[voice]` extra silently disables the record key.

```yaml
voice:
  enabled: true           # set false to disable F2 dictation even if deps are installed
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
| `enabled` | bool | `true` | Set `false` to hard-disable F2 dictation even when deps are installed. |
| `model` | string | `small` | Whisper model size: `tiny` / `base` / `small` / `medium` / `large-v3`. |
| `language` | string \| null | `ja` | ISO 639-1 language code. `""` or `null` enables auto-detection (less reliable for short clips). |
| `device` | string | `cpu` | Inference device: `cpu` or `cuda`. `auto` is not supported — it picks the wrong device on some Mac setups. |
| `compute_type` | string | `int8` | Quantisation: `int8` / `float16` / `float32`. |
| `sample_rate` | int | `16000` | Sample rate (Hz). Whisper expects 16 kHz mono — do not change. |
| `cpu_threads` | int | `4` | CPU threads for faster-whisper. `0` = OpenMP default. Pinning to 4 avoids OpenMP/Python-threading deadlocks on Apple Silicon. |
| `num_workers` | int | `1` | Parallel transcription streams. `1` keeps memory + thread usage low. |
| `max_duration_s` | float | `300.0` | Auto-cancel recordings longer than this (seconds). Prevents runaway memory growth from unattended recordings. |

## `multimodal` block

Controls how Reyn handles binary media (images from `web_fetch` / `read_file` / MCP servers) and where multimodal artefacts live on disk.

```yaml
multimodal:
  max_bytes: 5000000              # 5 MB — Anthropic per-image API limit
  on_oversize: ask                # ask | allow | deny
  media_dir: .reyn/media          # project-relative dir for image binaries
  tool_results_dir: .reyn/tool-results   # project-relative dir for tool-result dumps
  base_url: null                  # optional canonical URL prefix for cross-host path_ref
  model_capability_overrides: {}  # declared media capabilities for a proxied/uncataloged model
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `max_bytes` | int | `5000000` (5 MB) | Decoded-payload byte cap before the on-oversize gate fires. Counts the binary size (`len(response.content)` / `len(file_bytes)`), not the base64-encoded shape. |
| `on_oversize` | string | `ask` | What to do when a piece of media exceeds `max_bytes`: `ask` (prompt the user via the intervention bus with size + source info; yes loads the media, no drops it), `allow` (silently accept; use in trusted non-interactive pipelines), `deny` (silently reject; the op returns `status="denied"` — use in cost-sensitive contexts). |
| `media_dir` | string | `.reyn/media` | Project-relative directory for image binary storage. Files are flat-named with timestamp + chain-id + tool prefix so `ls -la` sorts chronologically. Operator-browseable and operator-deleteable. |
| `tool_results_dir` | string | `.reyn/tool-results` | Project-relative directory for text-y tool result dumps. |
| `base_url` | string \| null | `null` | Optional canonical URL prefix for cross-host `path_ref` consumption. When set (e.g. `"https://reyn.example.com"` from a deployed `reyn web`), saved artefacts carry a `url` field pointing at `<base_url>/agents/<agent>/tool-results/<artifact>` so A2A peers / MCP clients / browsers can fetch the body via the resources router. Unset → no `url` field minted (same-host fast-path only). |
| `model_capability_overrides` | `{model: {capability_field: bool}}` | `{}` | **(#5509)** Declares a model's media capability when litellm's own catalog doesn't know the model string at all. This is the ORDINARY case, not an edge case, for a proxy-routed deployment (a name like `openai/my-proxy-model` misses litellm's static catalog — `get_model_info` raises for it) — without a declaration, **every non-text attachment for that model silently degrades to a lossless path-ref instead of being embedded inline**, which reads to a user as "attachments stopped working". `capability_field` is NOT any litellm `get_model_info` field — only the ones reyn's own code actually queries (`reyn.llm.model_media_capability.QUERIED_CAPABILITY_FIELDS_BY_MODALITY`'s own values; today just `supports_vision`) — a wider litellm field would be accepted but silently do nothing. A one-time warning (`media_capability_unknown` in the log, naming the exact key to set) fires the first time this happens for a given `(model, capability_field)` pair. See `reyn.llm.model_media_capability`'s own module docstring for the full 3-state (supported / unsupported / unknown) resolution rule. Example: `{"openai/my-proxy-model": {"supports_vision": true}}`. |

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
