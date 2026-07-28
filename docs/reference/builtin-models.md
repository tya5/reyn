---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# Built-in model catalog

Reyn ships a built-in catalog of common model configurations pre-loaded into the model
namespace.  These entries let you reference well-known models by a short class name
without declaring them in `reyn.yaml`.

> **These are examples, not endorsements.**  The built-in catalog provides a convenient
> starting point.  Your `reyn.yaml` is always the source of truth.  Override any entry
> by declaring the same name under `models:`.

## Catalog entries

### `light`, `standard`, `strong`

```yaml
light:    gemini-flash-lite    # alias — extends: gemini-flash-lite
standard: gemini-flash-lite    # alias — extends: gemini-flash-lite
strong:   gemini-pro           # alias — extends: gemini-pro
```

The three generic tier names `ReynConfig.model` and `--model` document are
**aliases** into the concrete catalog below, not separate model definitions —
a project's own `reyn.yaml` normally redeclares these three under `models:`
(see `reference/config/reyn-yaml.md`), which overrides these built-ins with
the same override semantics as any other entry. Without a `reyn.yaml` at all
(or one whose `models:` block omits one of these three), the class still
resolves via this built-in alias rather than reaching LiteLLM as a bare,
unresolved class name (#3368).

#### Partially declared tiers are warned about

Because an omitted tier still resolves, an *incomplete* `models:` block would
otherwise be invisible: a project that maps `light` and `strong` but forgets
`standard` silently routes every default-class call to reyn's built-in default
instead of the provider it deliberately chose for the other two. So when
`models:` declares **some but not all** of the tiers, reyn logs a warning
naming the omitted tier, the model it fell back to, and the line to add:

```
reyn.yaml `models:` declares the light, strong tier(s) but omits standard —
the omitted tier(s) still resolve, via reyn's built-in defaults:
standard -> gemini/gemini-2.5-flash-lite. ... To take control, add under
`models:` in reyn.yaml:
  standard: gemini/gemini-2.5-flash-lite
```

Declaring **none** of the tiers is the normal zero-config case and is *not*
warned about — that is exactly what these aliases exist to serve. Declaring
**all** of them warns about nothing, since no tier is falling back.

### `claude-sonnet`

```yaml
model: anthropic/claude-3-7-sonnet
max_completion_tokens: 8192
```

General-purpose Claude Sonnet.  Good for most instruction-following tasks.

### `claude-sonnet-thinking`

```yaml
model: anthropic/claude-3-7-sonnet
max_completion_tokens: 16000
extra_body:
  thinking:
    type: enabled
    budget_tokens: 8000
```

Claude Sonnet with extended thinking enabled (`budget_tokens: 8000`).  Use this for
reasoning-heavy tasks.  Cost is roughly 2–3× `claude-sonnet` for the same output length.

To create a cost variant, use `extends`:

```yaml
models:
  reasoning-light:
    extends: claude-sonnet-thinking
    extra_body:
      thinking:
        budget_tokens: 4000   # overrides 8000; type: enabled is carried from base
```

### `claude-haiku`

```yaml
model: anthropic/claude-3-5-haiku
max_completion_tokens: 4096
```

Fast and cost-efficient Claude Haiku.  Best for simple extraction and classification tasks.

### `gpt-4o-mini`

```yaml
model: openai/gpt-4o-mini
```

OpenAI GPT-4o mini.  Low cost, high speed.

### `gpt-4o`

```yaml
model: openai/gpt-4o
```

OpenAI GPT-4o.  Strong general-purpose model.

### `gemini-flash-lite`

```yaml
model: gemini/gemini-2.5-flash-lite
reasoning_effort: low      # #1654: reasoning ON by default
```

Google Gemini 2.5 Flash Lite.  Very low cost. Ships with `reasoning_effort: low`
so reasoning/thinking is **on out of the box** (see the reasoning note below).

### `gemini-pro`

```yaml
model: gemini/gemini-2.5-pro
reasoning_effort: medium   # #1654: reasoning ON by default
```

Google Gemini 2.5 Pro.  High capability, suitable for strong-tier tasks. Ships
with `reasoning_effort: medium`.

### `gemini-3.1-flash-preview`

```yaml
model: gemini/gemini-3.1-flash-preview
reasoning_effort: low      # #1654: reasoning ON by default
```

Google Gemini 3.1 Flash Preview. Ships with `reasoning_effort: low`.

> **Reasoning on by default (#1654)** — the Gemini reasoning models above ship
> with a default `reasoning_effort`, and `chat.reasoning.{capture,display,
> continuity}` default on, so the model's reasoning text is produced, shown
> (collapsible), and carried across turns out of the box. **Cost note**: thinking
> tokens add to spend (low ≈ 1024, medium ≈ 2048 thinking-budget tokens/turn). To
> turn it off, set `reasoning_effort: none` on the model (or `disable`), or keep
> the budget but hide the text with `chat.reasoning.display: false`. **OpenAI-family
> caveat**: OpenAI reasoning models (o-series / GPT-5) often do NOT expose raw
> reasoning text (it is summarized/encrypted) — there `reasoning_effort` still
> controls the budget but the text display may be empty; prefer a Gemini model
> for visible reasoning text.

### `gemini-2.0-flash`

```yaml
model: gemini/gemini-2.0-flash
extra_body:
  thinking_config:
    thinking_budget: 0
```

Google Gemini 2.0 Flash with thinking disabled (`thinking_budget: 0`) for cost reduction.

> **LiteLLM / Gemini API note**: the `thinking_config.thinking_budget` parameter disables
> Gemini's thinking mode via LiteLLM's OpenAI-compatible shim.  If Gemini or LiteLLM
> changes this parameter name in a future release, update your `reyn.yaml` override and
> check the LiteLLM release notes.  This syntax is not guaranteed stable across provider
> API versions.

## Vendor-specific quirks

### `max_completion_tokens` vs `max_tokens`

The built-in catalog uses `max_completion_tokens` for Anthropic models, not `max_tokens`.

- `max_completion_tokens`: enforced at the API level by OpenAI o1+ and Anthropic.
  The provider refuses to generate more tokens than the limit, which makes it effective
  for hard cost control.
- `max_tokens`: a legacy soft hint.  Many providers ignore it; it has no enforcement
  power on OpenAI o1+ or Anthropic models.

Always prefer `max_completion_tokens` when you need a hard output cap.

### Anthropic thinking models

`claude-sonnet-thinking` sends `extra_body.thinking.{type, budget_tokens}` to the
Anthropic API via LiteLLM.  The `budget_tokens` value is the upper bound of reasoning
tokens; actual usage may be less.  Setting `budget_tokens` too low can degrade answer
quality on complex tasks.

### Reasoning on tool-bearing turns (Responses-API bridge — litellm-native)

A turn that carries **tools** *and* has `reasoning_effort` set is, for SOME
reasoning models, only valid on the OpenAI `/v1/responses` endpoint —
`/v1/chat/completions` rejects the combination with a 405 (#1678,
owner-confirmed on `gpt-5.4`). Reyn used to detect this shape itself and
rewrite the model string to a `responses/<model>` bridge marker (#1678,
provider-gated to OpenAI/Azure at #3325). **That manual bridge was deleted**
(#3288 follow-up, issue #3288 comment thread, owner-approved): **litellm
>= 1.89.3 ships its own automatic bridge**
(`litellm.main.responses_api_bridge_check`, upstream `BerriAI/litellm#23577`,
merged 2026-03-13 — before #1678 was even filed), entered from inside
`litellm.acompletion()` itself with no `responses/` prefix required from the
caller. Reyn now passes the resolved model straight to `litellm.acompletion`
unchanged; litellm decides internally whether to route to `/v1/responses`.

**Why delegate rather than keep reyn's own gate.** Investigation found reyn's
provider-allowlist bridge was strictly *wider* than litellm's own routing —
it fired for every OpenAI/Azure reasoning model (`o1`, `o3-mini`, …), none of
which were ever verified to actually need the bridge, whereas litellm's
routing is narrower and upstream-maintained, tracking future provider/model
changes automatically. Read directly from litellm's source
(`litellm/main.py::responses_api_bridge_check`), the actual condition is a
conjunction, not a single provider/family check:

```
custom_llm_provider in ("openai", "azure")
  AND is_model_gpt_5_model(model)
  AND reasoning_effort is not None
  AND (reasoning_summary is not None OR (is_gpt_5_4_plus_model(model) AND tools))
```

(plus `model_info.get("mode") == "responses"` as a separate, model-specific
trigger — see `o1-pro` below.) Measured per-model: `o1` / `o1-pro` / `o3-mini`
/ `gpt-4o-mini` all resolve `is_gpt_5_model=False` (never bridged this way —
`o1-pro` bridges instead via `mode == "responses"`); `gpt-5` / `gpt-5.1`
resolve `is_gpt_5_model=True` but need an explicit `reasoning_summary`, not
just `tools`; `gpt-5.4`-and-above resolve `is_gpt_5_4_plus_model=True`, where
`tools` alone (with `reasoning_effort` set) is sufficient — this is reyn's
own verified bug case. A bridge that's too wide is not the safe direction —
it's exactly the shape of the #3288 default-config regression (Gemini's
`tools + reasoning_effort` primary-reply shape got silently rewritten to an
unrecognized `responses/` model string, disabling token streaming) before
#3325 narrowed it. Deleting reyn's own gate removes an unverified, frozen
guess in favor of a narrower, upstream-maintained one.

litellm's bridge currently cannot map the `reasoning` output item some models
return, so a bridged call can still raise:

```
litellm.APIConnectionError: OpenAIException -
Unknown items in responses API response: [GenericResponseOutputItem(type='reasoning', ...)]
```

The reasoning text is present in the response — the bridge parser simply doesn't
map the `reasoning` item onto the chat-completions shape. This is present in both
the current and the latest litellm release, with no released fix; Reyn does not
ship a provider-specific workaround for it. This is unaffected by the delegation
above — it is a litellm-internal parsing gap either way.

**Do not set `litellm.route_all_chat_openai_to_responses = True`.** litellm
exposes this as a global opt-in (default `False`) that routes *every* OpenAI
chat call through `/v1/responses`, not just the tools+reasoning_effort combo
covered above. Turning it on reproduces the #3288 default-streaming
regression for the entire OpenAI provider: `_streaming_capable` cannot
recognize the bridged model shape, so streaming silently goes dark for every
OpenAI call, not just reasoning+tools ones. Reyn does not set this flag and
you should not either.

**If your endpoint 405s anyway.** Reyn no longer knows whether IT applied a
bridge (litellm decides internally now), but it still raises a
decision-enabling `ResponsesEndpointRequiredError` on an HTTP 405 for a
`tools + reasoning_effort` call **resolved to the OpenAI or Azure
provider** — naming both remedies: unset `reasoning_effort` for that agent,
or enable `/v1/responses` on your proxy. The provider scope matters:
litellm's bridge only ever fires for `openai`/`azure` (read directly from
`litellm.main.responses_api_bridge_check`'s source), so a 405 on e.g. a
Gemini call shaped this way is unrelated to `/v1/responses` — the error
would be actively misleading there, and does not fire. This is deliberately
kept as a safety net for litellm's narrower routing coverage WITHIN the
providers it actually bridges: if an OpenAI/Azure model needs the bridge but
litellm's heuristic doesn't (yet) cover it, the 405 surfaces as actionable
guidance instead of a raw dead-end.

**Unaffected paths:**

- **The default setup.** The `standard`/`light` classes (Gemini Flash Lite,
  which DOES ship with `reasoning_effort: low` by default — see #1654 above)
  are unaffected: Gemini's `reasoning_effort` maps to a native "thinking
  budget" parameter handled entirely on `/v1/chat/completions`, and litellm's
  bridge only engages for OpenAI-family reasoning models. Tool-bearing turns
  on the default config go through `/v1/chat/completions` as normal.
- **Non-tool chat with reasoning.** A reasoning-capable model *without* tools goes
  through `/v1/chat/completions`; reasoning survives and round-trips normally
  (surfaced as `reasoning_content` / `thinking_blocks`).

## Namespace and override semantics

The built-in catalog is merged into the model namespace **before** user entries, so
user-declared entries always win:

```yaml
# reyn.yaml
models:
  # Override built-in claude-sonnet with a project-specific variant.
  claude-sonnet:
    model: anthropic/claude-3-7-sonnet
    max_completion_tokens: 4096   # tighter budget for this project
```

## See also

- `reference/config/reyn-yaml.md` — `models:` block, `extends` syntax, deep merge
