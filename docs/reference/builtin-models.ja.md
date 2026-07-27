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

### ツールを伴う turn での reasoning（Responses-API bridge — litellm ネイティブ）

**tools** を伴い、`reasoning_effort` が設定された turn は、一部の reasoning
model では `/v1/responses` endpoint でのみ有効です — `/v1/chat/completions`
はこの組み合わせを 405 で拒否します（#1678、`gpt-5.4` で owner 確認済み）。
以前は reyn 自身がこの形状を検出し、model 文字列を `responses/<model>`
bridge marker に書き換えていました（#1678、#3325 で OpenAI/Azure provider に
gate）。**この手動 bridge は削除されました**（#3288 follow-up、issue #3288
コメントスレッド、owner 承認済み）: **litellm >= 1.89.3 が自前の automatic
bridge を持っています**（`litellm.main.responses_api_bridge_check`、upstream
`BerriAI/litellm#23577`、2026-03-13 merge — #1678 が file される前）。これは
`litellm.acompletion()` 内部から発火し、呼び出し側が `responses/` prefix を
付ける必要はありません。reyn は解決済みの model をそのまま
`litellm.acompletion` に渡し、`/v1/responses` へ route するかどうかは
litellm が内部で決定します。

**なぜ reyn 自身の gate ではなく委譲するか。** 調査の結果、reyn の
provider-allowlist bridge は litellm 自身の routing より **明らかに広い**
ことが分かりました — OpenAI/Azure の reasoning model（`o1`、`o3-mini` 等）
すべてに対して発火していましたが、そのうちどれも実際に bridge が必要かは
検証されていませんでした。一方 litellm の routing はより狭く（現状
gpt-5.4-family + tools + reasoning_effort、または
`model_info.mode == "responses"`）、upstream が保守するため将来の
provider/model 変化に自動的に追随します。広すぎる bridge は安全な方向では
ありません — それはまさに #3288 のデフォルト構成 regression（Gemini の
`tools + reasoning_effort` primary-reply 形状が未認識の `responses/` model
文字列に静かに書き換えられ、token streaming が無効化された）と同じ形です
（#3325 で狭められました）。reyn 自身の gate を削除することは、検証されて
いない凍結された推測を、より狭く upstream が保守する判断に置き換えることを
意味します。

litellm の bridge は現状、model が返す `reasoning` output item を map
できないため、bridge された call は次の error を raise することがあります:

```
litellm.APIConnectionError: OpenAIException -
Unknown items in responses API response: [GenericResponseOutputItem(type='reasoning', ...)]
```

reasoning テキストは response に含まれています — bridge parser がその `reasoning`
item を chat-completions の形に map しないだけです。これは current と latest 両方の
litellm release に存在し、released fix はありません。Reyn は provider 固有の回避を
作りません。これは委譲の有無に関わらず影響を受けません — litellm 内部の parsing
gap のためです。

**`litellm.route_all_chat_openai_to_responses = True` を設定しないでください。**
litellm はこれを global opt-in（既定 `False`）として公開しています。有効にすると
上記の tools+reasoning_effort の組み合わせだけでなく、OpenAI の chat call
**すべて**が `/v1/responses` を経由するようになります。これを有効にすると、
OpenAI provider 全体で #3288 のデフォルト streaming regression が再現します:
`_streaming_capable` が bridge された model 形状を認識できず、reasoning+tools
に限らず OpenAI の call すべてで streaming が静かに死にます。Reyn はこの flag
を設定しませんし、あなたも設定すべきではありません。

**endpoint が 405 を返す場合。** reyn はもう「自分が bridge したか」を知りません
（litellm が内部で決定するため）が、`tools + reasoning_effort` の call 形状
**かつ OpenAI または Azure provider に解決される** model で HTTP 405 が発生した
場合は、依然として decision-enabling な `ResponsesEndpointRequiredError` を
raise します — 両方の対処法を示します: その agent の `reasoning_effort` を
unset するか、proxy 側で `/v1/responses` を有効にするか。provider による
scope は重要です: litellm の bridge は `openai`/`azure` にしか発火しない
ため（`litellm.main.responses_api_bridge_check` の実装を直接確認済み）、
例えば Gemini の call がこの形状で 405 した場合、それは `/v1/responses` とは
無関係です — その場合この error は誤解を招くため raise されません。これは
litellm がカバーする provider の範囲内で、routing coverage が狭い場合の
safety net として意図的に残されています: OpenAI/Azure の model で bridge が
必要だが litellm の heuristic がまだカバーしていない場合、405 は raw な
dead-end ではなく actionable な guidance として表面化します。

**影響を受けないパス:**

- **デフォルト構成。** `standard`/`light` class（Gemini Flash Lite。#1654 の
  通り `reasoning_effort: low` がデフォルトで設定済み）は影響を受けません:
  Gemini の `reasoning_effort` はネイティブの thinking-budget パラメータに
  map され、`/v1/chat/completions` だけで完結し、litellm の bridge は
  OpenAI-family の reasoning model にのみ発火するためです。デフォルト構成の
  tool turn は通常通り `/v1/chat/completions` を通ります。
- **tool なしの chat + reasoning。** reasoning-capable model を tools *なし* で使うと
  `/v1/chat/completions` を通り、reasoning は survive して正常に round-trip します
  （`reasoning_content` / `thinking_blocks` として現れる）。

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
