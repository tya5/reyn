---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# Built-in model catalog

Reyn は標準的な model 設定を built-in catalog として ship、 model namespace に
pre-load しています。 これらの entry を使うと、 `reyn.yaml` で再宣言せずに短い
class 名で代表的な model を reference できます。

> **これらは example であり推奨ではありません**。 built-in catalog は便利な
> starting point を提供するもので、 真の source of truth は常に `reyn.yaml`
> です。 同名の entry を `models:` 配下で declare すれば override 可能。

## Catalog entries

### `claude-sonnet`

```yaml
model: anthropic/claude-3-7-sonnet
max_completion_tokens: 8192
```

汎用 Claude Sonnet。 大半の instruction-following task に適している。

### `claude-sonnet-thinking`

```yaml
model: anthropic/claude-3-7-sonnet
max_completion_tokens: 16000
extra_body:
  thinking:
    type: enabled
    budget_tokens: 8000
```

extended thinking 有効化済 (`budget_tokens: 8000`) の Claude Sonnet。 reasoning が
重い task に使う。 cost は同 output 長で `claude-sonnet` のおよそ 2〜3 倍。

cost variant を作るには `extends` を使う:

```yaml
models:
  reasoning-light:
    extends: claude-sonnet-thinking
    extra_body:
      thinking:
        budget_tokens: 4000   # 8000 を override; type: enabled は base から carry
```

### `claude-haiku`

```yaml
model: anthropic/claude-3-5-haiku
max_completion_tokens: 4096
```

高速で cost-efficient な Claude Haiku。 単純な抽出 / 分類 task に最適。

### `gpt-4o-mini`

```yaml
model: openai/gpt-4o-mini
```

OpenAI GPT-4o mini。 低 cost + 高速。

### `gpt-4o`

```yaml
model: openai/gpt-4o
```

OpenAI GPT-4o。 強力な汎用 model。

### `gemini-flash-lite`

```yaml
model: openai/gemini-2.5-flash-lite
```

Google Gemini 2.5 Flash Lite (= OpenAI 互換 shim 経由)。 非常に低 cost。

### `gemini-3.1-flash-preview`

```yaml
model: openai/gemini-3.1-flash-preview
```

Google Gemini 3.1 Flash Preview (= OpenAI 互換 shim 経由)。

### `gemini-2.0-flash`

```yaml
model: openai/gemini-2.0-flash
extra_body:
  thinking_config:
    thinking_budget: 0
```

thinking 無効化 (= `thinking_budget: 0`) で cost 削減した Google Gemini 2.0 Flash。

> **LiteLLM / Gemini API note**: `thinking_config.thinking_budget` パラメタは
> LiteLLM の OpenAI 互換 shim 経由で Gemini の thinking mode を無効化する。
> 将来 Gemini / LiteLLM がこのパラメタ名を変更したら、 `reyn.yaml` の override
> を update し LiteLLM release notes を確認すること。 この syntax は provider
> API version 跨ぎで stable と保証されない。

## Vendor-specific quirks

### `max_completion_tokens` vs `max_tokens`

built-in catalog は Anthropic model で `max_completion_tokens` を使い、
`max_tokens` は使わない:

- `max_completion_tokens`: OpenAI o1+ と Anthropic の API level で **enforce
  される**。 provider が制限を超えて出力しないことを保証するので、 hard cost
  control に effective
- `max_tokens`: legacy の soft hint。 多くの provider が ignore する、 OpenAI
  o1+ や Anthropic では矯正力なし

hard output cap が必要なときは常に `max_completion_tokens` を優先する。

### Anthropic thinking model

`claude-sonnet-thinking` は `extra_body.thinking.{type, budget_tokens}` を LiteLLM
経由で Anthropic API に送る。 `budget_tokens` は reasoning token の上限値で、
実際の使用は少ないこともある。 複雑な task で `budget_tokens` を低く設定すると
answer 品質が落ちる可能性あり。

### ツールを伴う turn での reasoning（Responses-API bridge）

**tools** を伴い、かつ reasoning-capable model に `reasoning_effort` が設定された
turn は、litellm の Responses-API bridge（`responses/<model>`）を通ります。litellm
の bridge は現状、model が返す `reasoning` output item を map できないため、call が
次の error を raise します:

```
litellm.APIConnectionError: OpenAIException -
Unknown items in responses API response: [GenericResponseOutputItem(type='reasoning', ...)]
```

reasoning テキストは response に含まれています — bridge parser がその `reasoning`
item を chat-completions の形に map しないだけです。これは current と latest 両方の
litellm release に存在し、released fix はありません。Reyn は provider 固有の回避を
作りません。

**発火する条件 — 狭い opt-in の組み合わせ。** 両方が成立する必要があります:

1. **tool を伴う** purpose（例: router。その turn は常に tools を持つ）が
   **reasoning-capable** model を指す — `model_class_by_purpose: router: strong`、
   またはデフォルトの `model` class を capable model に設定。**かつ**
2. その model に `reasoning_effort` が設定されている。

**影響を受けないパス:**

- **デフォルト構成。** `standard` class（Gemini Flash Lite）は `reasoning_effort`
  なしで tool turn を捌くので、bridge でなく `/v1/chat/completions` を通り error
  なし。（Flash Lite は tool turn で reasoning-dormant でもある。）
- **tool なしの chat + reasoning。** reasoning-capable model を tools *なし* で使うと
  `/v1/chat/completions` を通り、reasoning は survive して正常に round-trip します
  （`reasoning_content` / `thinking_blocks` として現れる）。

**回避するには**、tool を伴う turn を担う reasoning-capable model に
`reasoning_effort` を設定しない — または tool を伴う purpose を non-reasoning model
に置く。tool なしの chat パスの reasoning は影響を受けません。

## Namespace + override semantics

built-in catalog は user entry の **前に** model namespace に merge されるので、
user-declared entry が常に勝つ:

```yaml
# reyn.yaml
models:
  # built-in claude-sonnet を project 固有 variant で override
  claude-sonnet:
    model: anthropic/claude-3-7-sonnet
    max_completion_tokens: 4096   # この project では tighter な budget
```

## See also

- `reference/config/reyn-yaml.md` — `models:` block、 `extends` syntax、 deep merge
