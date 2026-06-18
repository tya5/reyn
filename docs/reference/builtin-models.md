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

### Reasoning on tool-bearing turns (Responses-API bridge)

A turn that carries **tools** *and* has `reasoning_effort` set on a
reasoning-capable model is routed through litellm's Responses-API bridge
(`responses/<model>`). litellm's bridge currently cannot map the `reasoning`
output item the model returns, so the call raises:

```
litellm.APIConnectionError: OpenAIException -
Unknown items in responses API response: [GenericResponseOutputItem(type='reasoning', ...)]
```

The reasoning text is present in the response — the bridge parser simply doesn't
map the `reasoning` item onto the chat-completions shape. This is present in both
the current and the latest litellm release, with no released fix; Reyn does not
ship a provider-specific workaround for it.

**When it bites — a narrow, opt-in combination.** Both must hold:

1. a **tool-bearing** purpose (e.g. the router, whose turns always carry tools)
   is pointed at a **reasoning-capable** model — `model_class_by_purpose: router:
   strong`, or the default `model` class set to a capable model; **and**
2. `reasoning_effort` is set on that model.

**Unaffected paths:**

- **The default setup.** The `standard` class (Gemini Flash Lite) handles
  tool-bearing turns with no `reasoning_effort`, so they go through
  `/v1/chat/completions`, not the bridge — no error. (Flash Lite is also
  reasoning-dormant on tool turns.)
- **Non-tool chat with reasoning.** A reasoning-capable model *without* tools goes
  through `/v1/chat/completions`; reasoning survives and round-trips normally
  (surfaced as `reasoning_content` / `thinking_blocks`).

**To avoid it**, keep `reasoning_effort` off any reasoning-capable model that
serves tool-bearing turns — or keep tool-bearing purposes on a non-reasoning
model. Reasoning on the non-tool chat path is unaffected.

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
