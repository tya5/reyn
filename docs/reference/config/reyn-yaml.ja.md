---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# `reyn.yaml`

プロジェクトレベルの設定。git にチェックインします。個人設定のオーバーライドは `reyn.local.yaml`（gitignored）または `~/.reyn/config.yaml` に記述します。

## 最小限の例

```yaml
llm:
  model: standard
  models:
    light:    gemini-flash-lite
    standard: openai/gpt-4o
    strong:   anthropic/claude-3-5-sonnet-20241022
```

## トップレベルキー

**「書く面 / 再読込」列の読み方。** ほとんどのキーは `reyn.yaml` /
`reyn.local.yaml`（＝ **PRJ スコープ**）にしか書けず、`load_config` が
**起動時に一度だけ**読みます — 実行中に編集しても、**再起動するまで効きません**。

例外はランタイム可変なレジストリ群で、これらは `.reyn/config/<名前>.yaml` にも
書けます。**`.reyn/config/` 側に書いた分だけがターン境界で読み直され**（= hot
reload）、`reyn.yaml` 側に書いた同じキーは他と同じく再起動待ちです。
**ファイル分割そのものが write-gate の境界**であるため（#2073）、hot reload の
ローダは `reyn.yaml` を構造的に読みません — 「hot reload される設定」を増やしたい
場合は、`reyn.yaml` に書き足すのではなく `.reyn/config/` 側へ置きます。

**どのキーがこの例外に入るかの一次の出所は `src/reyn/config/loader.py` の
`_HOT_RELOAD_FILES`** です。下の表の各行はそこから引いていますが、**個数や
名前の一覧をここから読み取らず、`_HOT_RELOAD_FILES` を読んでください** — 追加
されてもこの散文は自動追随しません。

⚠️ **例外の例外**: `composers` は `hooks` と同じ 4 層の結合（`reyn.yaml` ∪
`.reyn/config/hooks.yaml` ∪ エージェントごと ∪ セッションごと）で読まれますが、
**hot reload されません** — 追加・削除は次のセッション開始で効きます。

なお、この表が扱う軸は **PRJ（`reyn.yaml`）と `.reyn/config/` の 2 面だけ**です。
`hooks` / `composers` / permission 系はさらに**エージェント面**
（`.reyn/agents/<名前>/`、`.reyn/capability_profiles/<名前>.yaml`）と
**セッション面**（`<session-state-dir>/config.yaml`）にも書けます。
そちらは [permission-model](../../concepts/runtime/permission-model.md) を参照。

**置き場の原理(#4174 T7)**: ある設定が**その subsystem 自身のコードパスからしか
読まれない**なら、所有ブロックの下にネストします（例: `embedding.cost_warn_threshold`
— embedding/indexing パイプライン内の 1 箇所でしか読まれない）。**単一の subsystem
に収まらない複数の場所から読まれる**なら、トップレベルの独立ブロックにします（例:
`cost_warn` — セッションが何をしていようと、chat router が `/model` 切り替え時と
セッション起動時のどちらでも読む）。**2 つのキー名に共通する部分文字列があっても、
それは「同じ設定である」証拠にはなりません** — 名前ではなく、誰がその値を何箇所から
読むかで判断します。この原則を書くきっかけになった具体例（名前は似ているが無関係な
`cost_warn` と `embedding.cost_warn_threshold`）は EN 版の
[`cost_warn` block](reyn-yaml.md#cost_warn-block) 節を参照してください。

| キー | 型 | 書く面 / 再読込 | 説明 |
|-----|------|-----|-------------|
| `output_language` | 文字列 | PRJ のみ・**再起動** | デフォルトの出力言語コード（例: `en`、`ja`）。`--output-language` でオーバーライド。 |
| `safety` | マップ | PRJ のみ・**再起動** | ランタイムの上限**と content 層の防御**: ループ検出上限、タイムアウト、上限超過時ポリシー、非信頼コンテンツの threat scan + fence（`safety.threat_scan`、FP-0050）、LLM spawn ツリーの上限（`safety.spawn`、DoS ガード）。以下参照。 |
| `cost` | マップ | PRJ のみ・**再起動** | バジェット上限とレート制限（エージェントごと、日次、月次）。以下参照。 |
| `web_fetch` | マップ | PRJ のみ・**再起動** | `web_fetch` ツールと MCP レジストリ呼び出しの SSL 設定。以下参照。 |
| `gateway` | マップ | PRJ のみ・**再起動** | `reyn web` ゲートウェイ自身の設定: 認証モデル、WebSocket 受信フレーム上限、マウントするサーフェス。旧 `web:` キー（`web_fetch` と同居していた）から分割。以下参照。 |
| `sandbox` | マップ | PRJ のみ・**再起動** | バックエンド選択（`backend`）、非対応プラットフォームポリシー（`on_unsupported`）、強制モード（`mode`: compat / strict / custom）、agent-level サンドボックスポリシー（`policy`）。以下参照。 |
| `embedding` | マップ | PRJ のみ・**再起動** | RAG 埋め込み: マスタースイッチ（`enabled`）、モデルクラス、バッチサイズと並列度、リトライ / バックオフ / タイムアウト、トークナイザ、コスト警告閾値。以下参照。 |
| `chat` | マップ | PRJ のみ・**再起動** | チャットセッションのランタイム設定: 履歴の圧縮、reasoning（"thinking"）テキストの扱い、対話レンダラ（`render_mode`）、TUI の gutter、body の neutralize、許可する画像 URL スキーム。以下参照。 |
| `voice` | マップ | PRJ のみ・**再起動** | ⚠️ 現在利用不可(consumerなし)。以下参照。 |
| `audit_events` | マップ | PRJ のみ・**再起動** | `.reyn/events` 配下の P6 **audit-event** ファイルのローテーション（サイズ / 経過時間 / 掃除周期）。WAL-event でも hook-event でもありません。以下参照。 |
| `observability` | マップ | PRJ のみ・**再起動** | P6 監査イベントの OpenTelemetry (OTLP) エクスポート（オプトイン）。デフォルトは無効。以下参照。 |
| `tool_use` | マップ | PRJ のみ・**再起動** | chat レイヤーの tool-use scheme x transport セレクタ（`scheme`、`transport`）。以下参照。 |
| `mcp` | マップ | 両方（`.reyn/config/mcp.yaml` 側は **hot reload**） | MCP サーバー定義。以下参照。 |
| `agent_id` | 文字列 | PRJ のみ・**再起動** | エージェントの**識別子**— P6 監査証跡と送信 HTTP ヘッダーに刻まれます。**エージェントの定義・設定はしません**（エージェント定義は `.reyn/agents/<名前>/`）。以下参照。 |
| `auth` | マップ | PRJ のみ・**再起動** | `reyn auth login` 用の OAuth プロバイダー設定。以下参照。 |
| `cron` | マップ | 両方（`.reyn/config/cron.yaml` 側は **hot reload**） | スケジュール付きスキル実行。以下参照。 |
| `external_transports` | マップ | PRJ のみ・**再起動** | チャット向け受信トランスポート → MCP ツールルーティング（Slack / LINE / Discord など）。以下参照。 |
| `multimodal` | マップ | PRJ のみ・**再起動** | バイナリメディア（画像・音声）のサイズ上限、超過時の挙動、アーティファクト保存先、およびそれらを配信する `base_url`。以下参照。 |
| `permissions` | マップ | PRJ のみ・**再起動** | デフォルトの Permission ポリシー。以下参照。 |
| `project_context_path` | 文字列 | PRJ のみ・**再起動** | すべての Phase システムプロンプトに注入する Markdown ファイル。未設定（デフォルト）: cross-tool 標準を auto-resolve — `AGENTS.md` があればそれ、なければ `REYN.md`（legacy fallback）。明示パスで 1 ファイルに固定、`""` で無効化。下記の注記参照。 |
| `llm` | マップ | PRJ のみ・**再起動** | LLM 層の設定: モデル選択（`llm.model` デフォルトクラス、`llm.models` クラス → LiteLLM 文字列マップ、`llm.model_class_by_purpose` 用途別上書き、`llm.api_base` プロキシ URL、`llm.prompt_cache_enabled`）に加え、ルーティング（#1829）とリトライ（#1835）。以下参照。#4174 T3: `model` / `models` / `model_class_by_purpose` / `api_base` / `prompt_cache_enabled` は同名のトップレベルキーからここへ移動しました（形は同じ、ネストが変わっただけ）。 |
| `delegation` | マップ | PRJ のみ・**再起動** | エージェント間委任のポリシー（#2081）。 |
| `cost_warn` | マップ | PRJ のみ・**再起動** | 高コストモデルのゲート（#1830 / FP-0052）: 選択前に警告し、名前に反して**ブロックもできます**（`cost_warn.block_on_high_cost`）。以下参照。 |
| `offload` | マップ | PRJ のみ・**再起動** | tool 結果のサイズゲートの opt-in スイッチ。 |
| `render_template` | マップ | PRJ のみ・**再起動** | `render_template` op の出力上限（FP-0055 / #2679）。 |
| `fs_watch` | マップ | PRJ のみ・**再起動** | オペレータが宣言するファイル監視パス（#2608 H4）。 |
| `hooks` | リスト | 両方（`.reyn/config/hooks.yaml` 側は **hot reload**） | hook 定義。アクションは 4 種: `template_push` / `exec` / `exec_capture` / `pipeline_launch`。空（既定）→ HookDispatcher は no-op。以下参照。 |
| `composers` | リスト | 両方（ただし **hot reload されない**・再起動） | composer 定義。空（既定）→ `start_composers` は呼ばれません。 |
| `skills` | マップ | 両方（`.reyn/config/skills.yaml` 側は **hot reload**） | skill 宣言。設定層をまたいで名前でマージ（明示エントリが衝突時に勝つ）。 |
| `pipelines` | マップ | 両方（`.reyn/config/pipelines.yaml` 側は **hot reload**） | pipeline 宣言。`skills` と同じ union-merge。 |
| `presentations` | マップ | 両方（`.reyn/config/presentations.yaml` 側は **hot reload**） | presentation テンプレート宣言。`skills` / `pipelines` と同じ union-merge。 |

> **プロジェクトコンテキストファイル（`project_context_path`）。** 未設定のとき
> Reyn は `AGENTS.md` を読みます — Claude Code・Codex・opencode 等も読む cross-tool
> 標準です — ので、それらツールと共有するプロジェクトが Reyn 専用ファイルなしでその
> まま動きます。`AGENTS.md` が無ければ `REYN.md`（legacy）に fallback。最初に存在する
> ファイルが優先され、present-but-empty な `AGENTS.md` は authoritative（`REYN.md` へは
> fall through しません）。
>
> **移行。** 既存の `REYN.md` プロジェクトは無変更で動作し続けます。新規は `AGENTS.md`
> を推奨。標準に依らず特定ファイルに固定するには `project_context_path` にそのパスを
> 設定、`""` でプロジェクトコンテキストを一切注入しない。

## `llm` ブロック

LLM 層の設定: モデル選択（`llm.model` / `llm.models` / `llm.model_class_by_purpose`
— この節 — に加え `llm.api_base` / `llm.prompt_cache_enabled`）、そして
`llm.router` / `llm.retry`（opt-in litellm.Router + Reyn 自前リトライの
バックオフタイミング）。#4174 T3: モデル選択は同名のトップレベルキー
`model:` / `models:` / `model_class_by_purpose:` からここへ移動しました
（形は同じ、ネストが変わっただけ）。

### `llm.models` ブロック

`llm.models:` の各エントリはクラス名を LiteLLM モデル文字列 **または** per-class LLM パラメータを宣言する dict にマップします。

### モデルクラス と モデル名 — 解決ルール

config には2種類の位置があり、逆のルールに従います。同じルールが補完側 `models:` ブロック **と** `embedding.classes:` ブロックの両方に適用されます。

- **クラス位置**（クラスへの *参照*）：`model`、per-agent / per-phase / per-op のモデル上書き、`embedding.default_class`。これらは **closed-world** — 値は `models:` / `embedding.classes:` に存在するクラス（または組み込み tier: `light` / `standard` / `strong`）を指さなければなりません。既知クラスでない値は、リテラルモデルとして黙って素通しされません：
  - オペレータ config（reyn.yaml の `model:`）は後方互換のリテラル素通しを維持（`openai/gpt-4o` を直接書ける）；
  - **skill/op 由来**のモデル（`op.model`）が既知クラスでない場合は **reject** され、runtime モデルにフォールバック（警告1件）します。これにより skill・LLM 由来の文字列が proxy config を迂回できません — モデル選択の単一の真実源は proxy config です。
- **名前位置**（モデルの *定義*）：`models:` / `embedding.classes:` エントリ内の `model:` 値。名前は `provider/model`（例：`openai/gpt-4o`、litellm proxy 背後のローカルモデルなら `openai/nomic-embed-text`）であるべきです。`/` のない bare 名は許容されます（一部の LiteLLM 文字列は bare）が、ロード時に **警告** します — 解決が誤ルートする場合は prefix を追加してください。

一言で：**`_class` / tier 位置はクラス名（closed-world）、`model` 位置は `provider/model`（検証付き）。どちらも受け付ける位置はない。**

### str 形式 — リテラル（後方互換）

str 値に **`/` が含まれる** 場合、LiteLLM モデル文字列として直接使用されます：

```yaml
llm:
  models:
    light:    gemini-flash-lite
    standard: openai/gpt-4o
    strong:   anthropic/claude-3-5-sonnet-20241022
```

str 形式を使用している既存の `reyn.yaml` はすべて変更なしで動作します。

### str 形式 — クラス参照省略形（新規）

str 値に **`/` が含まれない** 場合、`{extends: <name>}` の省略形として扱われます。
名前はフラット namespace（ユーザーエントリ + built-in カタログ）で解決されます：

```yaml
llm:
  models:
    standard: claude-sonnet-thinking     # 等価: standard: {extends: claude-sonnet-thinking}
```

不明な省略形（ユーザーエントリにも built-in にも存在しない名前）は起動エラーになります。

### dict 形式 — plain kwargs

```yaml
llm:
  models:
    standard: gemini-flash-lite   # str 形式も dict エントリと併用可能

    strong:
      model: anthropic/claude-3-7-sonnet      # 必須
      temperature: 0.0
      max_completion_tokens: 16000             # max_tokens より推奨 — 下記注意
      extra_body:
        thinking:
          type: enabled
          budget_tokens: 8000
```

| フィールド | 必須 | 説明 |
|-------|----------|-------------|
| `model` | はい | LiteLLM モデル文字列。 |
| `temperature` | いいえ | litellm に渡すサンプリング温度。 |
| `max_completion_tokens` | いいえ | **推奨** 最大出力トークン数（OpenAI o1+ およびほとんどのプロバイダーで強制）。 |
| `max_tokens` | いいえ | レガシーのソフトヒント — 多くのプロバイダーが無視する。`max_completion_tokens` を推奨。 |
| `top_p` | いいえ | litellm に渡す top-p サンプリング。 |
| `extra_body` | いいえ | プロバイダー固有のペイロード（例：推論モデルの `thinking`）。 |
| `reasoning_effort` | いいえ | モデルの推論バジェット: `minimal` / `low` / `medium` / `high` / `disable` / `none`。**ロード時にバリデーション**（下記参照）。 |
| `extends` | いいえ | 名前付きクラスから継承し、オーバーライドを deep merge（下記参照）。 |
| `stream` / `stream_options` | いいえ | **設定不可。** ストリーミングするかどうかは reyn 自身が決めます（呼び出しごとの litellm capability query）。どちらのキーもモデル定義に書くと**ロード時に reject**されます（下記参照）。 |
| *（その他のフィールド）* | いいえ | litellm にそのまま渡されます（パススルーポリシー）。 |

> **コスト制限**: `max_tokens` ではなく `max_completion_tokens` を使用してください。`max_tokens` は多くのプロバイダーが無視するレガシーのソフトヒントです。`max_completion_tokens` は API レベルで強制されます（OpenAI o1+ および Anthropic モデル）。

**フィールドポリシー**: `model` のみ必須です。ほとんどのフィールドはバリデーションなしで `litellm.acompletion` に直接渡されます（未知のフィールドも silent に転送されます — future-proof）。タイポは reyn エラーではなく silent な litellm 失敗を引き起こします。ロード時にバリデーションされる例外は2つ: `reasoning_effort`（下記）と `stream` / `stream_options`（下記） — 後者は値チェックではなく**完全に reject**されます。

### `stream` / `stream_options`（設定不可）

ストリーミングするかどうかは reyn が決めます — 単一の completion 経路
（`recorded_acompletion`）内で行われる、呼び出しごとの litellm capability
query です。モデル定義側で `stream` あるいは `stream_options` を宣言しても
ストリーミングは有効になりません（capability query が依然として決定します）
— そのキーが kwargs のパススルーに乗って、query が選んだどちらかの分岐に
届くだけです。非ストリーミング分岐では、これによって litellm がストリーム
オブジェクトを返し、reyn がそれを完了した返答として読んでしまい、
以下のように表面化していました:

```
EmptyLLMResponseError: LLM returned a 200 response with empty choices
(model=...); provider response: <litellm...CustomStreamWrapper object...>
```

両キーとも litellm に届く前に**コンフィグロード時に reject**されます
（`ValueError`、fail-fast）:

```yaml
llm:
  models:
    strong:
      model: openai/gpt-5
      stream: true   # ロード時に ValueError — このキーを削除してください
```

### `reasoning_effort`（モデルごとの推論バジェット）

モデルが回答前にどれだけ「思考」するかを設定します。分かりやすさのためモデル定義ごとに宣言します:

```yaml
llm:
  models:
    light:
      model: gemini/gemini-2.5-flash-lite
      reasoning_effort: low      # minimal | low | medium | high | disable | none
```

- **有効な値**: `minimal`, `low`, `medium`, `high`, `disable`, `none`。無効な値は litellm の呼び出し中ではなく**コンフィグロード時に fail-fast**（不正値を示す明確な `ValueError`）。
- **ネイティブマッピング**: 値は litellm にネイティブに渡され、プロバイダー自身の推論バジェットにマッピングされます。Gemini（例: `gemini-2.5-flash-lite`）では: `low` → thinking budget 1024、`medium` → 2048、`high` → 4096、`minimal` → モデル固有（flash-lite は 512）、`disable` / `none` → 0。手書きの `extra_body` は不要です。
- **`extra_body` の thinking 設定とは排他**: `reasoning_effort` *が* thinking-budget の制御なので、同一モデルに `reasoning_effort` と `extra_body` の thinking 設定の両方を宣言すると**ロード時に reject**されます（どちらか一方を選択）。

> **既知の挙動 — 推論テキストは表示されません。** 非ゼロの `reasoning_effort` はプロバイダーの `includeThoughts=true` を設定するため、レスポンスに推論／思考テキストが含まれます。reyn は現在、推論 vs 出力の**トークン数**の内訳のみを記録し、推論テキスト自体は捕捉・表示しません。したがって `reasoning_effort` を有効にすると、思考を表示せずに推論トークンのコストが発生します。

> **既知の挙動 — tool-use パスで thinking が再有効化されます。** reyn は thinking を強制 off にせず、プロバイダーのデフォルト（Gemini 2.5 は off）に従います。`reasoning_effort` を設定すると thinking が on になり、Gemini で以前 parallel-tools + thinking の相互作用があったマルチターン tool-use パス（Gemini #17949）でも有効になります。tool-heavy なエージェントで有効化する場合はモデルの挙動を検証してください。

> **プロキシ経由（openai 互換）での透過**: litellm プロキシ経由でルーティングする場合、reyn は `reasoning_effort` を `allowed_openai_params` でホワイトリスト化し、プロキシに転送します（プロキシがプロバイダーのネイティブ thinking budget にマッピング）。追加設定は不要です。

**Skill / Phase 側オーバーライド**: サポートしていません。Operator config（`reyn.yaml`）が LLM パラメータの唯一の source of truth です。Skill 作者はクラス名のみを指定します（例：`model_class: strong`）。

**マージ順**: Reyn が管理する設定（`timeout`、`num_retries`、プロキシルーティング）は operator 宣言の kwargs より常に優先されます。

### dict 形式 — `extends` フィールド（新規）

`extends` を使用して別のクラスから継承し、特定のフィールドをオーバーライドできます。
参照される名前はフラット namespace（ユーザーエントリ + built-in カタログ）で解決されます。

```yaml
llm:
  models:
    # claude-sonnet-thinking built-in を継承し、budget_tokens を 8000 → 4000 に変更。
    # extra_body.thinking.type: enabled は base から引き継がれます（deep merge）。
    reasoning-light:
      extends: claude-sonnet-thinking
      extra_body:
        thinking:
          budget_tokens: 4000

    # マルチレベル: reasoning-heavy は上で定義した reasoning-light を extends。
    reasoning-heavy:
      extends: reasoning-light
      extra_body:
        thinking:
          budget_tokens: 16000
      max_completion_tokens: 32000
```

**Deep merge**: ネストした dict は再帰的にマージされます。`extra_body.thinking` の下に指定したキーのみがオーバーライドされ、兄弟キー（例：`type: enabled`）は base から引き継がれます。スカラーとリストは置換されます（マージされません）。

**マルチレベルチェーン**: 任意の深さが許可されます。Reyn は起動時にチェーン全体を解決します。

**サイクル検出**: 循環する `extends` 参照（例：`A extends B, B extends A`）は起動時に検出され、設定エラーが発生します。

**不明な参照**: namespace に存在しない名前の参照は起動エラーになります — `reyn.yaml` が
`light` / `standard` / `strong` 自体をマップしていない場合、それらも対象です（次項参照）。

### Built-in カタログは無い

Reyn には、具体的な provider/model のターゲットを持つ built-in カタログは**ありません** —
`light` / `standard` / `strong` は reyn 自身の語彙（コスト順に並んだ 3 つの標準 tier）ですが、
それぞれが実際に何を指すかは、上記の `llm.models:` マッピング次第です。`reyn init` が生成
する `reyn.yaml` には出発点となるマッピングが書き込まれます — provider に合わせて編集して
ください。有効なマッピングが無いクラス（`reyn.yaml` も `reyn.local.yaml` も無い、または
`models:` ブロックがそのクラスを省略している）は、欠けているクラス名を明示した起動エラーに
なります — reyn はどのクラス（tier もカスタムも）についても、隠れた既定値へ黙ってフォール
バックしません。

## `chat` ブロック

チャットセッションのランタイム設定。`chat.compaction` はチャット履歴の圧縮を制御します（`reyn.local.yaml.example` 参照）。`chat.reasoning` はモデルの推論／"thinking" テキストの扱いを制御します。

```yaml
chat:
  reasoning:
    continuity: true      # reasoning を履歴に永続化 + 直近ターンを次プロンプトに replay
    display: true         # reasoning を UI（TUI + web、折りたたみ可）に表示
    recent_turns: 3       # replay する reasoning のターン数; <=0 = 無制限
```

### `chat.reasoning` フィールド

プロバイダーの `reasoning_content` のキャプチャは **常時 ON**。これらの knob はその後の扱いを制御します。`continuity` と `display` はともにデフォルト **ON**。

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `continuity` | bool | `true` | reasoning を履歴に永続化 **かつ** 直近ターンの reasoning を次ターンの system prompt に replay（cross-user-turn の reasoning continuity、`act_turn_reasoning` を踏襲したテキストセクション）。opt-out で永続化 + replay を無効化。 |
| `display` | bool | `true` | reasoning を UI（TUI + web、折りたたみ可）に表示。opt-out で非表示。`continuity` とは独立。 |
| `recent_turns` | int | `3` | `continuity` 時に replay する直近 reasoning のターン数。`<= 0`（例: `0` / `-1`）= 無制限（全保持）。Gemini ではプロバイダー側の auto-filter が無いため bounding が重要（reasoning が蓄積し全量課金される）。 |

> **プロバイダー注記**: Gemini-via-proxy では reasoning はテキストセクションとして replay され（モデルは prompt 内で参照）、wire-shape の assistant message からは `reasoning_content` を strip します（litellm の vertex transformation がネイティブにも emit して double-inject になるのを防ぐ）。Anthropic/DeepSeek の direct-API は tool-use パスでネイティブ `reasoning_content` round-trip を要求します（litellm が wire 上に残っていれば auto 処理）— 既知のプロバイダー依存で、ここでは未実装（proxy + Gemini 前提）。

## `safety` ブロック

停止条件の統合ネームスペース。各値は対応する CLI フラグで呼び出しごとにオーバーライドできます。（旧トップレベル `limits:` キーは廃止。`safety:` が唯一の信頼できる情報源です。）

```yaml
safety:
  loop:
    max_router_calls_per_turn: 3 # ユーザーターンごとのチャットルーター呼び出し数
    max_router_iterations: 5    # ユーザーターンごとの LLM ツール呼び出しイテレーション数 (CLI --max-iterations で上書き可)
    max_agent_hops: 3            # 最大委譲深度
  timeout:
    llm_call_seconds: 60         # 呼び出しごとの HTTP タイムアウト (--llm-timeout)
    llm_max_retries: 3           # 呼び出しごとの一時的エラーのリトライ数 (--llm-max-retries)
    chain_seconds: 60            # デリゲート返答待機時間
  on_limit:
    mode: interactive            # interactive | unattended | auto_extend
    auto_extend_times: 1         # （auto_extend モード）自動延長回数
    ask_timeout_seconds: 0       # （interactive モード）ユーザープロンプトのタイムアウト; 0 = 無制限待機
```

### `safety.loop` フィールド

| パス | 型 | デフォルト | CLI フラグ | 説明 |
|------|------|---------|---------|-------------|
| `safety.loop.max_router_calls_per_turn` | int | `3` | — | ユーザーターンごとのチャットルーター呼び出し数。`0` = 無制限。 |
| `safety.loop.max_router_iterations` | int | `5` | `--max-iterations` | ユーザーターンごとの LLM ツール呼び出しイテレーション上限。CLI `--max-iterations` が指定された場合はそちらが優先。`reyn run-once` のデフォルトは 80。 |
| `safety.loop.max_agent_hops` | int | `3` | — | 最大委譲深度（ユーザー → A → B → C = 3 ホップ）。 |

### `safety.timeout` フィールド

| パス | 型 | デフォルト | CLI フラグ | 説明 |
|------|------|---------|---------|-------------|
| `safety.timeout.llm_call_seconds` | float（秒） | `60` | `--llm-timeout` | LiteLLM に渡される呼び出しごとの HTTP タイムアウト。 |
| `safety.timeout.llm_max_retries` | int | `3` | `--llm-max-retries` | LLM 呼び出しごとの一時的エラーのリトライ数（LiteLLM 指数バックオフ）。 |
| `safety.timeout.chain_seconds` | float（秒） | `60` | — | マルチエージェントチェーンがデリゲート返答を待機する時間。上限超過後にランタイムが上流エラーを生成。`0` = 無効。 |

### `safety.on_limit` フィールド

| パス | 型 | デフォルト | 説明 |
|------|------|---------|-------------|
| `safety.on_limit.mode` | 文字列 | `interactive` | ループ/タイムアウト上限発動時の動作。`interactive`（デフォルト） — `ask_user` でユーザーに延長許可を確認。ヘッドレス（bus=None / 非 TTY）は自動的に abort へ短絡。`unattended` — 即時中止（CI / cron / スクリプト実行向けのオプトイン）。`auto_extend` — `auto_extend_times` 回自動延長後に中止。 |
| `safety.on_limit.auto_extend_times` | int | `1` | abort フォールスルーまでの自動延長回数。`mode: auto_extend` 時のみ使用。 |
| `safety.on_limit.ask_timeout_seconds` | float（秒） | `0` | `interactive` モードでユーザー返答を待機する時間。`0`（デフォルト） = 無制限待機、正の値 = ウィンドウ経過で partial data として abort。 |

## `tool_use` ブロック {#tool_use-block}

chat レイヤーの tool-use **scheme x transport** セレクタ（FP-0066 P4b, #3247）。
tool-use は直交する 2 つの軸に分解されます: `scheme` は **presentation** —
能力が LLM にどう提示・発見されるか（`category` / `enumerate-all` /
`retrieval`）— であり、`transport` はモデルが選択したアクションをどう
表現するか（`tool_calls` / `content_fence`）です。解決された
`(scheme, transport)` の組が、登録済みの `ToolUseScheme`（tool の提示・
ディスパッチ方法を差し替え可能にする機構）を選択します。

```yaml
tool_use:
  scheme: enumerate-all       # デフォルト
  transport: tool_calls       # デフォルト
  universal_wrappers_enabled: true    # デフォルト; false でオプトアウト
```

| キー | 型 | デフォルト | 説明 |
|-----|------|---------|-------------|
| `scheme` | 文字列 | `enumerate-all` | トップレベル chat レイヤーの presentation: `category` / `enumerate-all` / `retrieval`。**デフォルト `enumerate-all`** — アクションをフラットに列挙し LLM が直接呼び出せるようにする（`invoke_action` 名のハルシネーションを防ぎ、direct tool-use が ~30%→100% に改善）。少数サーフェス / 多数ツールのカタログには `category` を設定（discover-then-call）。 |
| `transport` | 文字列 | `tool_calls` | モデルが選択したアクションをどう表現するか: `tool_calls`（ネイティブ tool-calling）または `content_fence`（応答テキスト内のフェンス付きコードとしてアクションを表現 — CodeAct）。 |
| `universal_wrappers_enabled` | bool | `true` | **#4552 PR-3 — `action_retrieval.universal_wrappers_enabled` からここへ移動**（architect 裁定: `tool_use`/presentation-scheme の性質であり、retrieval 設定ではない）。`scheme` が `universal-category` に解決される layer について、`true`（デフォルト）は 4 universal wrapper（`list_actions` / `search_actions` / `describe_action` / `invoke_action`）のみをその layer の `tools=` に出す。legacy per-kind tool（`invoke_skill` / `call_mcp_tool` 等）はその layer で LLM に surface されず、wrapper の backing handler として残存。`search_actions` は `embedding.enabled` で別途ゲート（#4564 — このフラグはどの scheme でも `search_actions` の可視性に一切影響しない）。`false` 設定でその layer の wrapper surface 自体を無効化（= legacy のみが addressing path）。`scheme` が `enumerate-all`/`retrieval` である layer には影響しない。`scheme` が `universal-category` でないのにこのフラグを明示的に `true` にしても効果は無く、`reyn config validate` がその組み合わせを報告する（#4231(C)）。 |

`list_actions` / `describe_action` / `invoke_action` wrapper の完全な意味論（カテゴリ discovery、エラー復帰 `suggestions`、weak-model landing design）は [Concepts: universal catalog](../../concepts/tools-integrations/universal-catalog.ja.md) を参照。

上記の軸の値の組み合わせは、現時点ですべて実装済みです:

| `scheme` \ `transport` | `tool_calls` | `content_fence` |
|---|---|---|
| `category` | `universal-category` | ラッパーの code-API |
| `enumerate-all` | `enumerate-all`（デフォルト） | CodeAct |
| `retrieval` | `retrieval` | 検索起点の code-API |

この表に無い組み合わせ — reyn に存在しない `scheme` / `transport` 名 — は、
黙って default にフォールバックしたり受理されたりせず、**config-parse 時に**
分かりやすいエラーを送出します。CodeAct は `scheme: enumerate-all` +
`transport: content_fence` で到達します — `enumerate-all` と同じ全件フラット
カタログを、ネイティブ tool call の代わりにフェンス付きコードとして表現した
ものであり、独立した `scheme` 名ではありません。`retrieval` はさらに
`embedding.enabled: true` を要求します（FP-0066 §7）。

`scheme: category` + `transport: content_fence` は CodeAct の小サーフェス版
です。モデルはフェンス付き Python を書きますが、見せられる関数はカタログの
**ラッパー**（`list_actions` / `describe_action` / `invoke_action`）と base
tools だけなので、CodeAct と違ってシステムプロンプトがカタログと共に増えません。
呼び出しは
`result = invoke_action(action_name="read_file", args={"path": "README.md"})`
の形になります。

```yaml
tool_use:
  scheme: category
  transport: content_fence
```

**使いどころ**: 弱い / 低コストモデルで JSON tool call よりコードを書かせた方が
良く、**かつ**カタログが大きく全アクションの列挙がトークン的に高すぎる場合。
CodeAct（`enumerate-all` + `content_fence`）は後者を捨てています。コード内の
呼び出しは同等の JSON 呼び出しと同じ exclude + permission ゲートを通り、さらに
サンドボックスで封じ込められます。

`scheme: retrieval` + `transport: content_fence` は**検索起点の code-API** です。
見せられる関数は `search_actions` / `describe_action` / `invoke_action` と base
tools で、`list_actions` は**含まれません** — この presentation における discovery
は列挙ではなく検索だからです。1 ターンはこうなります:

```python
hits = search_actions(query="read a file")
result = invoke_action(action_name="read_file", args={"path": "README.md"})
```

```yaml
tool_use:
  scheme: retrieval
  transport: content_fence
  # embedding.enabled: true が必要
```

`tool_calls` 側の retrieval セルとはパラダイムではなく**コスト**が違います。
あちらでは絞り込みに 1 往復かかります（`tools=` ペイロードは LLM 呼び出しの
*間* にしか変えられないので、OS が一致アクションを再提示する）。こちらでは
検索結果はスニペット内の単なる値なので、検索と呼び出しが同一ターンで済みます。
`category` + `content_fence` より優先するのは、カタログが大きくカテゴリからの
ブラウズが入口として適切でなく、モデルに「やりたいこと」を記述させたい場合です。
埋め込みインデックスが未準備のときは、空を返す検索を見せる代わりにフラット
カタログの列挙へフォールバックします。

旧来の単一 `tool_use.chat` key は**削除済み**です（clean-break、compat
alias 無し）。`tool_use.chat` を残したままの `reyn.yaml` は、config-load
時に `scheme` / `transport` への移行を名指すエラーで**大きく失敗**します
— 黙って無視されることはありません。旧 `chat: codeact` は `scheme:
enumerate-all` + `transport: content_fence` に、旧 `chat:
universal-category` は `scheme: category`（`transport` はデフォルトの
`tool_calls` のまま）になります — `category` は presentation 軸の名前で、
登録済みの `universal-category` scheme に解決されます。

scheme は `tools=` payload の構築方法、SP の tool-use 指示、LLM 応答の解釈方法、
ディスパッチ方法のすべてを所有します — そのため `scheme` / `transport` を
入れ替えると、OS 側の変更なしに chat レイヤーの tool-use ループ全体が変わります。

各 scheme が何をするか、**どれをいつ選ぶか**（`enumerate-all` / `retrieval` /
CodeAct vs デフォルト）については
[Tool-Use Schemes](../../concepts/tools-integrations/tool-use-schemes.ja.md) を参照してください。

## `web_fetch` ブロック

`web_fetch` ツールと MCP パッケージレジストリの SSL 設定。

`web.fetch:` から改名（#4174 T4）— 旧 `web:` キーはこれ（web_fetch ツール自身の設定）と、
無関係な `reyn web` ゲートウェイ自身の設定（`gateway:` に分割）を同居させていました。

```yaml
web_fetch:
  verify_ssl: true     # true | false | 省略（デフォルト: 環境変数チェーン）
  ca_bundle: /path/to/ca-bundle.pem   # 省略可; カスタム CA バンドル
  max_download_bytes: 10485760        # ワイヤバイト上限（デフォルト 10MB）
  allow_private_ips: false            # SSRF: プライベート IP への opt-in（デフォルト deny）
```

優先度チェーン（高い順）:

| 優先度 | 条件 | 有効な SSL 設定 |
|--------|------|----------------|
| 1 | `web_fetch.ca_bundle` 設定あり | カスタム CA バンドルファイル（`verify=<path>`） |
| 2 | `web_fetch.verify_ssl: false` | SSL 検証を無効化（`verify=False`）— **管理された環境のみ** |
| 3 | `web_fetch.verify_ssl: true` | SSL 検証を強制（`verify=True`） |
| 4 | 両方省略 | フォールスルー: `SSL_VERIFY` 環境変数 → `litellm.ssl_verify` → `SSL_CERT_FILE` → `True` |

`verify_ssl` と `ca_bundle` は MCP レジストリの HTTP 呼び出し（パッケージインストール）にも適用されます。

`web_fetch.max_download_bytes`（int, デフォルト `10485760` = 10MB）は `web_fetch` がワイヤから読み取るレスポンスの最大バイト数。`Content-Length` がこの値を超えるレスポンスは本文ダウンロード前に拒否され、chunked / 長さ不明の本文はストリームが上限を超えた時点で中断されます（ステータス `too_large`）。悪意ある / 暴走 URL による無制限な本文のメモリ枯渇を防ぎます。`<= 0` / 非整数はデフォルトにフォールバック。

`web_fetch.allow_private_ips`（bool, デフォルト `false`）は SSRF 対策。`true` のとき `web_fetch` / `safe.http` がプライベート RFC1918/ULA アドレスへ到達できます（エンタープライズの内部 fetch 向け opt-in）。link-local / クラウドメタデータ（`169.254.169.254`）/ ループバックはこのフラグに関わらず**常に**拒否されます。HTTP リダイレクトは hop ごとに再検証（allowlist + IP-deny）されるため、allowlist 済みホストが内部ターゲットへリダイレクトすることはできません。`REYN_FETCH_ALLOW_PRIVATE_IPS` 環境変数にもエクスポートされ、safe.http サブプロセス + レジストリクライアントが同じ opt-in を参照します。

> ℹ️ **#4274**: `web_fetch.*` は現在、実際のチャットセッションの `web_fetch` op 実行に配線されています（`SessionFactoryConfig.web_fetch_config` → `Session` → router `OpContext`）。これが入るまでは、パース・validate は通っても実際の `web_fetch` 呼び出しには届きませんでした — #4174 T4 の改名が作った・悪化させたものではない既存のギャップでした。`verify_ssl: false` や `allow_private_ips: true` のような非デフォルト値を設定したまま気づいていなかった環境では、これから実際に効くようになります。

## `gateway` ブロック

`reyn web` ゲートウェイ自身の設定。

`web:` から分割（#4174 T4）— 上の `web_fetch` を参照してください。

```yaml
gateway:
  ws_max_size: 16777216  # WebSocket インバウンドフレーム上限（デフォルト 16MB）
```

`gateway.ws_max_size`（int, デフォルト `16777216` = 16MB）は `reyn web` ゲートウェイが受け付ける単一 WebSocket インバウンドフレームの最大バイト数。サーバーライブラリの暗黙デフォルトに依存せず上限を明示的に固定するため、ライブラリアップグレード後も bound が維持されます。operator は tighten / loosen 可能。`<= 0` / 非整数はデフォルトにフォールバック。

## `sandbox` ブロック

`sandboxed_exec` op + OS の in-process file/http ゲートのバックエンド選択・非対応プラットフォームポリシー・agent-level サンドボックスポリシー。

```yaml
sandbox:
  backend: auto          # auto | seatbelt | landlock | noop
  on_unsupported: warn   # warn | error | ignore
  policy:                # オプション — agent-level（オペレータ）サンドボックスポリシー
    network: true
    write_paths: ["{{workspace}}", "/tmp"]
    deny_subprocess: false
    env_deny_names: []
    timeout_seconds: 600
```

> ℹ️ **`write_paths` を除く全軸が完全 compat をデフォルトとします**（owner ruling、#3901）:
> `network`/`deny_subprocess`/`read_deny_paths`/`write_deny_paths`/`env_deny_names` はすべて
> 「追加制限なし」から始まります — サンドボックスの役割は許可された操作の**裏側**を bound する
> ことであり、起動元シェルが既にできることを再決定することではありません。`write_paths` だけは
> デフォルトで閉じています: これはカーネルバックエンドが直接消費する、オペレーターが事前に
> 知り得ない値（「この op はこのディレクトリが必要」）なので、安全な compat デフォルトが
> ありません。
>
> **`write_deny_paths` のエントリは、重なる `write_paths` の許可より常に優先されます**
> （`read_deny_paths` も広い読み込みサーフェスに対して同様に、独立して — 2つの軸は別フィールド
> で、それぞれ自分の軸のみを deny します、#3901）。Seatbelt バックエンドでは deny ルールが
> `write_paths` の allow ルールの**後に** emit され、SBPL は last-match-wins のため、
> `write_deny_paths` に列挙したパスを包含する広い `write_paths`（`~`・`$HOME`・`/`）を書いても、
> そのパスは書き込み用には開きません。OS は `sandbox_policy_narrowed` audit-event を出して
> 縮小を可視化します（#2978）。credential 位置（`~/.ssh`・`~/.aws`・`~/.gnupg` 等）を保護したい
> 場合は `read_deny_paths` に明示的に列挙してください（両軸で保護したい場合は `write_deny_paths`
> にも）— #3901 以前と異なり、これはもうデフォルトではありません。オペレーターが opt-in する
> 値です。それでも `write_paths` は最小限のディレクトリに絞ってください。

| キー | 型 | デフォルト | 説明 |
|-----|------|---------|-------------|
| `backend` | 文字列 | `auto` | 強制バックエンド。`auto` は OS が選択: macOS < 26 → `seatbelt`（sandbox-exec SBPL）、Linux ≥ 5.13 かつ `sandbox-linux` extra インストール済み → `landlock`（+ オプションの seccomp-BPF）、その他 → `noop`（監査のみ、強制なし）。明示的な値で特定バックエンドを強制できます。 |
| `on_unsupported` | 文字列 | `warn` | 使用可能な OS サンドボックスバックエンドが無い場合のポリシー — 要求バックエンドがこのプラットフォームで利用不可の場合に加え、選択されたバックエンドが**封じ込め self-test に失敗した場合**（= 存在するが deny を発火しない。そのバックエンドは「存在しない」場合とまったく同じに扱われる）も含む。`warn` は WARNING をログに記録して `noop` にフォールバック。`error` は `RuntimeError` を発生（強制が必須な本番環境のフェイルファスト。存在するが不活性なバックエンドに対しても効く）。`ignore` はサイレントにフォールバック。 |
| `policy` | マップ | _なし_ | **agent-level（オペレータ）サンドボックスポリシー**。設定すると、サンドボックス op に適用される決定的ポリシーになり、かつ `network`/`subprocess`/`env` 軸について OS の in-process file/http ゲートの permission 積（`∩`）の `SandboxLayer` に畳み込まれます — op 宣言のフィールドに **優先（WINS）** するため、スキルや LLM が緩めることはできません。`write_paths`（および read/write deny リスト）はこの交差に**参加しません** — op が必要とするディレクトリはオペレーターが事前に知り得ない値なので、カーネルバックエンドが直接消費します（#3901 PR-B ③）。省略時（デフォルト）は **agent-level の制限なし**: `SandboxLayer` は恒等（`⊤`）のままで op レベルのフィールドが従来通り支配します。サンドボックス認可はオペレータ/run の関心事です。サブキーは以下参照。 |

### `sandbox.policy` サブキー

`sandbox.policy` が存在する場合、`SandboxPolicy` フィールドを反映します。未知のキーは config ロード時に拒否されます。

| キー | 型 | デフォルト | 説明 |
|-----|------|---------|-------------|
| `network` | bool | `true`（compat） | サンドボックスプロセスからの外向きネットワークを許可。主要な外部流出ゲート — `deny_subprocess`/`env_deny_names` と並んで permission 交差に引き続き参加する（下の path 軸とは異なり operator が明示宣言する値なので）。config で allow された host でも `network: false` の下では拒否されます。 |
| `write_paths` | list[文字列] | `[]` | プロセスが書き込み可能なパス（厳密なガード）— デフォルトで閉じている唯一のフィールド（オペレーターが事前に知り得ない値のため、安全な compat 床が無い）。書き込みは読み取りを含む。`~` は展開される。 |
| `read_deny_paths` | list[文字列] | `[]`（compat） | 広読み込みサーフェスから拒否する機密パス（多層防御、**opt-in**）。deny-after-allow をサポートするバックエンド（Seatbelt）のみ適用。許可リストのみのバックエンド（Landlock、read-deny プリミティブが無い）では非対応。#3901 以前は OS レベルの機密パス7件がデフォルトだった — その保護を戻すには明示的に設定します。読み込み軸のみを deny — 書き込み軸は `write_deny_paths` を参照。 |
| `write_deny_paths` | list[文字列] | `[]` | 書き込み軸専用の deny リスト（#3901）、`read_deny_paths` と対をなす。`write_paths` のエントリがこれらと重なる・包含する場合でも deny を無効化しない — Seatbelt 上では deny が常に勝ち（#2978）、`sandbox_policy_narrowed` audit-event が縮小を記録します。書き込み軸のみを deny。 |
| `deny_subprocess` | bool | `false`（compat） | 子プロセスの spawn を deny するか — #3901 以前の `allow_subprocess` の deny-list 形の逆（owner decision 2026-07-22, #3202: UX-blocking な軸は deny-by-default ではなく opt-in restrict）。適用（enforced）— on の時 `process-fork` を deny。 |
| `env_deny_names` | list[文字列] | `[]`（compat） | サンドボックスプロセスへ引き渡さない環境変数名 — #3901 以前の `env_passthrough` allowlist の deny-list 形の逆。デフォルト（空）は環境全体が引き渡される、つまり起動元シェルと同じ信頼レベルを意味します。 |
| `timeout_seconds` | int | `120`（#3903①、2026-08-11 — 以前は `60`） | LLM の `exec` 呼び出しが override を指定しない場合の前景ウォールクロックタイムアウト。`max_timeout_seconds` を超えて設定すると config load が失敗する。 |
| `max_timeout_seconds` | int | `600`（#3903①） | LLM が per-call で要求できる `timeout`（`exec` の任意引数）の上限 — **operator が制御**、ハードコード値ではない。上限を超える要求は静かに切り詰めず拒否し、実際に設定された上限を名指しする。`600` より下げれば LLM が要求できる範囲が実際に狭まる。LLM がこれを広げることはできない。 |

[リファレンス: control-ir — `sandboxed_exec`](../runtime/control-ir.md#sandboxed_exec) で op スキーマとバックエンド選択の詳細を参照してください。

## `agent_id`

監査証跡と HTTP ヘッダー伝播のためのランタイムエージェント識別子。

```yaml
agent_id: "reyn/acme/code-review-agent"  # デフォルト: reyn/<hostname>
```

トップレベルの単純なスカラー値（#4174 T5 — 旧 `agent: {id: ...}` 名前空間から
フラット化。そのブロックはフィールドを 1 つしか持たなかったため、名前空間は
構造を増やさず間接参照だけを増やしていた）。

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `agent_id` | 文字列 | `reyn/<hostname>` | この Reyn インスタンスの安定識別子。すべての P6 イベントペイロードに `agent_id` としてスタンプされ、MCP / A2A / 外部 HTTP リクエストの送信時に `X-Reyn-Agent-Id` ヘッダーとして付与される（SOC2 / ISO27001 / METI v1.1 監査パターン）。推奨フォーマット: `reyn/<org>/<role>`（operator 定義）。空文字列を指定した場合はデフォルトにフォールバックし、空の `agent_id` がイベントやヘッダーに漏れるのを防ぐ。 |

デフォルト `reyn/<hostname>` により、フレッシュインストールでも operator の設定なしに使用可能な識別子が付与されます。マルチエージェントフリートや安定したロール単位の識別子が必要なエンタープライズデプロイでは `reyn.yaml` でオーバーライドしてください。

[コンセプト: マルチエージェント — Agent ID 伝播](../../concepts/multi-agent/multi-agent.md) でクロスエージェントトレースと A2A ヘッダー転送の詳細を参照してください。

## `observability` ブロック

P6 監査イベントストリームを OpenTelemetry (OTLP) の span / metric / log record
としてエクスポートする、オプトインのサーフェス。**デフォルトは無効** —
エンドポイント未設定ならエクスポーターは attach されず、OTEL 無しのビルドと
バイト単位で同一の挙動になります。lossy かつ fire-and-forget な downstream で
あり、`.reyn/events` や WAL には一切書き込まないため、recovery と replay は OTEL
から独立しています。

```yaml
observability:
  otel:
    endpoint: "http://localhost:4318"     # OTLP HTTP ベース URL; "" で無効
    headers:
      Authorization: "Bearer ${OTEL_TOKEN}"
    service_name: "reyn"
    capture_content: false                # SR3: 生の prompt/response はデフォルト OFF
```

### `observability.otel` フィールド

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `otel.endpoint` | 文字列 | `""` | OTLP HTTP ベース URL（例: `http://localhost:4318`）。空 = 未 attach。標準の `OTEL_EXPORTER_OTLP_ENDPOINT` 環境変数がフォールバックとして尊重されるため、環境変数のみで有効化できます。 |
| `otel.headers` | マップ | `{}` | リクエストごとの HTTP ヘッダー（認証トークン等）。値は `${VAR}` 環境変数展開をサポート。 |
| `otel.service_name` | 文字列 | `reyn` | コレクターへ報告する `service.name` リソース属性。 |
| `otel.capture_content` | bool | `false` | GenAI content-capture ゲート。`false`（デフォルト）は ref と token/cost カウントのみ — 生の prompt/response body は span/log に出しません。`true` で content capture にオプトイン（信頼できるコレクター限定）。 |

OTEL SDK が必要です: `pip install reyn[observability]`。SDK 未インストールで
エンドポイントを設定した場合は一度だけ警告ログを出し、未 attach（fail-open）の
まま — セッションには影響しません。イベント → span/metric/log の完全なマッピング、
pin された GenAI convention バージョン、fail-open / recovery-independence 保証は
[リファレンス: observability (OTEL エクスポート)](../runtime/observability.md)
を参照してください。

## `auth` ブロック

`reyn auth login` 用の OAuth プロバイダー設定。`auth.providers` 以下の各名前付きエントリが RFC 8628 Device Authorization Grant プロバイダーを定義します。デフォルトは空であり、operator が認証対象のプロバイダーを宣言します。

```yaml
auth:
  providers:
    github:
      client_id: "${secret:github_oauth_client_id}"
      device_authorization_url: "https://github.com/login/device/code"
      token_url: "https://github.com/login/oauth/access_token"
      scopes: [repo, user]
      # client_secret 省略可 — PKCE のみ / public client の場合は省略
      client_secret: "${secret:github_oauth_client_secret}"
    google:
      client_id: "...apps.googleusercontent.com"
      device_authorization_url: "https://oauth2.googleapis.com/device/code"
      token_url: "https://oauth2.googleapis.com/token"
      scopes: [openid, email]
      client_secret: "${secret:google_oauth_client_secret}"
      # audience: Auth0 等の一部プロバイダーで必要
```

### `auth.providers.<name>` フィールド

| フィールド | 必須 | 説明 |
|-------|----------|-------------|
| `client_id` | はい | プロバイダーが発行した OAuth クライアント識別子。 |
| `device_authorization_url` | はい | `device_code`、`user_code`、`verification_uri` を返すエンドポイント（RFC 8628 §3.1）。 |
| `token_url` | はい | ユーザーが認可を完了した後に access / refresh トークンを発行するエンドポイント（RFC 8628 §3.4）。 |
| `scopes` | はい（リスト） | リクエストする OAuth スコープ。プロバイダーがスコープを必要としない場合は `[]` を渡す。 |
| `client_secret` | いいえ | コンフィデンシャルクライアント用。PKCE のみ / public client では省略可（RFC 6749 §2.3.1 にて installed app での省略を許可）。 |
| `audience` | いいえ | 一部プロバイダー（Auth0 等）で必要な API audience 識別子。GitHub や Google 等では省略する。 |

`${secret:<key>}` の値はコンフィグロード時に `~/.reyn/secrets.env` から解決されます。保存には `reyn secret set <key>` を使用します。

関連情報:

- [Reference: `reyn auth`](../../reference/cli/auth.md) — `reyn auth login/list/revoke` コマンド
- [コンセプト: シークレット管理](../../concepts/runtime/secret-handling.md) — OAuth ライフサイクルと認証情報スコープ
- [コンセプト: マルチエージェント](../../concepts/multi-agent/multi-agent.md) — エージェント識別子伝播

## `cron` ブロック

定期的なメッセージ配信をスケジュールします。スケジューラーは `reyn web` の一部（= FastAPI lifespan で起動）として、または `reyn cron run` 経由のフォアグラウンドプロセスとして実行されます。

```yaml
cron:
  jobs:
    - name: morning_news
      to: news_agent            # 宛先エージェント名
      message: "今日の主要ニュースをまとめて"
      schedule: "0 9 * * *"     # 毎日 09:00
      enabled: true

    - name: weekly_ops_report
      to: ops_agent
      message: "weekly ops report"
      schedule: "0 9 * * MON"   # 月曜 09:00
      enabled: true
```

### フィールド

- **`name`** (必須) — ジョブ識別子。スケジュール内で一意である必要があります
- **`to`** (必須) — 宛先エージェント名。メッセージは `sender="cron:<name>"` 属性でそのエージェントの inbox に配信されます
- **`message`** (必須) — 宛先エージェントに配信される自由形式テキスト
- **`schedule`** (必須) — 5 フィールドの cron 式
  （分 / 時 / 日 / 月 / 曜日）
- **`notify`** (省略可) — オプトインの無人通知チャンネル
- **`input`** (省略可、デフォルト `{}`) — ジョブに付随する追加の入力辞書
- **`enabled`** (省略可、デフォルト `true`) — `false` にすると設定にエントリを保持したままスケジューリングをスキップします

> レガシーなスキルベースジョブ（`skill` 名のみ）はサポートされなくなりました（skill runtime は削除済み）。旧 `cron.yaml` にそのようなエントリが残っていても、load 時に warn+skip され、reject されません。

### 関連情報

- `docs/reference/cli/cron.md` — `reyn cron run/list/status`
- `docs/guide/for-users/monitor-and-improve-with-cron.md` — `cron.jobs` と
  `.reyn/events/` を組み合わせて監視・改善エージェントを起こす方法（EN のみ）

## `permissions` ブロック

プロジェクト全体のケイパビリティデフォルト。`skill.md` の Skill ごとの Permission がこれらをオーバーライドします。

```yaml
permissions:
  exec: deny             # deny | ask | allow — `exec` ツールの事前承認キー
                          # （#3226 Phase 3 で `shell` から改名。clean-break、
                          # alias なし — 既存の reyn.yaml の `shell:` は `exec:` へ）
  file.read:  [".reyn/", "src/stdlib/"]   # フラットな dotted key — `file:` とネストした
  file.write: [".reyn/state/", "reyn/local/"]  # {read:, write:} は読まれません。
                                                # PermissionDecl.from_dict() が見るのは
                                                # ここに示すフラットなキーのみです。
  python:                  # LIST（{function, mode} のエントリ列）としてしか読まれません
    - function: compute     # — マッピング（`{safe: allow}`）は素通りされ 1 バイトも
      mode: safe             # 読まれません。用途は唯一つ: `mode: unsafe`（撤去済み）を
                              # 弾いて load を fail させること。実行時の権限は一切
                              # 付与しません — python ステップはこのキーに関わらず常に
                              # サンドボックス化されます。
```

MCP サーバーのインストールも同じ方式でゲートされます — `file.write`（宣言的パスリスト、上記と同じ）＋ `http.get`（宣言的ホストリスト）で、`permissions.mcp_install` の bool ではありません。下記「MCP install」を参照（そちらは blanket `allow`/`deny` スカラーで、ここで示したリスト形とは同じキーの別の使い方です）。

### MCP install

レガシーな `permissions.mcp_install: ask | allow | deny` bool 軸は削除されました。MCP install は他の OS 全体と同じリスト軸でゲートされます:

```yaml
# reyn.yaml — install permissions は file.write + http.get で表現
permissions:
  file.write: allow      # .reyn/config/mcp.yaml（= install 対象）への blanket allow
  web.fetch: allow        # レジストリ fetch への blanket allow（= レガシー alias）
```

より細かい制御は skill の `skill.md` が正規のパス・ホストを宣言する形で行います。`startup_guard` が skill+host ごとに初回だけインタラクティブプロンプトを出し、以降はランタイムチェックがサイレントになります（= デフォルトゾーン外のパスは `file.write` モデル、ホストは `http.get` per-host）。

| やりたいこと | 新しい形 |
|------|-----------|
| プロジェクト全体で install を全面ブロック | `.reyn/config/mcp.yaml` パスへの `file.write: deny`、またはレジストリホストへの `web.fetch: deny` |
| プロンプトなしで install を許可 | プロジェクトスコープで `file.write: allow` と `web.fetch: allow` |
| 特定ホストのみ許可 | skill が `http.get: [{host: "..."}]` を明示的に宣言。wildcard `["*"]` は per-host プロンプトに委ねる |

エンタープライズパターン — プライベート / 企業レジストリを宣言的 config か env-var override で指す:

```yaml
# reyn.yaml（プロジェクトスコープ — git にコミット）
mcp:
  registries:
    - https://mcp-registry.internal.acme.com/    # プライベートレジストリを先頭に
    - https://registry.modelcontextprotocol.io/   # パブリックフォールバック
permissions:
  web.fetch: allow       # レジストリ fetch への blanket allow
  file.write: allow      # .reyn/config/mcp.yaml への書き込み blanket allow
```

完全な Permission 文法は `reference/config/permissions.md` に記載されています。

## `${VAR}` interpolation {#var-interpolation}

`reyn.yaml`（または `reyn.local.yaml` / `~/.reyn/config.yaml`）の任意のセクションの任意の文字列フィールドで、`${VAR}` 構文を使って環境変数を参照できます。変数は起動時、`~/.reyn/secrets.env` を環境変数にロードした後に `os.environ` から解決されます（詳細は [コンセプト: シークレット管理](../../concepts/runtime/secret-handling.md) 参照）。

```yaml
# reyn.yaml — ${VAR} はすべての文字列フィールドで使用可能
llm:
  models:
    default-sonnet:
      model: claude-sonnet-4-5
      api_key: ${ANTHROPIC_API_KEY}          # LLM API キー — secrets.env またはシェルから解決
      extra_body:
        headers:
          Authorization: ${LITELLM_PROXY_TOKEN}
  api_base: ${LITELLM_API_BASE}            # LiteLLM プロキシ URL（#4174 T3: litellm: ではなく llm: にネスト）

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

解決ルール：

- `${VAR}` — 環境変数の値に展開されます。未定義の場合は警告を出して `""` に展開されます（ハードエラーにはなりません）。
- `$$` — リテラルの `$` 記号（エスケープ）。
- すべての YAML セクションのすべての文字列フィールドをネストした dict やリストも含めて再帰的にスキャンします。
- シェルの環境変数は `~/.reyn/secrets.env` の値より優先されます。

`~/.reyn/secrets.env` を管理するには `reyn secret set` / `reyn secret list` / `reyn secret clear` を使用します（[Reference: `reyn secret`](../../reference/cli/secret.md) 参照）。

## API キー

API キーとトークンは環境変数から来なければなりません。`reyn.yaml` にリテラル値を書かないでください。推奨パターン：

1. 一度だけ値を保存: `reyn secret set ANTHROPIC_API_KEY`
2. `reyn.yaml` で参照: `api_key: ${ANTHROPIC_API_KEY}`

`reyn.yaml` や `reyn.local.yaml` にトークン値をインラインで貼り付けないでください。これらは git にコミットされ、リポジトリへのアクセス権を持つすべての人が読めます。

## `llm` ブロック

### プロキシ / `llm.api_base`

モデルをローカルの LiteLLM プロキシ経由でルーティングする場合は、URL を `reyn.yaml` ではなく `reyn.local.yaml`（gitignored）に書きます。環境変数の参照も使えます：

```yaml
# reyn.local.yaml
llm:
  api_base: ${LITELLM_API_BASE}    # または直接書く: http://localhost:4000
```

### `llm.prompt_cache_enabled`

デフォルト `true`。システムプロンプトに Anthropic 形式の `cache_control`
マーカーを付与し、プロンプトキャッシュ対応プロバイダー（Anthropic、AWS
Bedrock Claude）がプレフィックスを再利用できるようにします。対応しない
プロバイダー（Gemini / OpenAI プロキシ）はこのマーカーを無視して素通しします。

```yaml
llm:
  prompt_cache_enabled: true
```

## 解決順序

各設定について、Reyn は（優先度が低い方から、後の層が前を上書き）マージします:

1. **組み込みデフォルト** — reyn 同梱の値（例: `llm.model: standard`）。
2. `~/.reyn/config.yaml`（ユーザーグローバル）
3. `reyn.yaml`（プロジェクト、コミット対象）
4. `reyn.local.yaml`（プロジェクト、gitignored — マシンローカルの上書き + `reyn config set` が書いた値）
5. `<project>/.reyn/config/mcp.yaml`（動的 MCP server レジストリ）— **`mcp.servers` セクションについて最後にマージ**。`reyn mcp install` が追加した server が、`reyn.yaml` / `reyn.local.yaml` で手書きした `mcp.servers` を上書きします。
6. `<project>/.reyn/config/cron.yaml`（動的 cron レジストリ）— **`cron.jobs` セクションについて最後にマージ**。ランタイム登録 job が name 衝突時に `reyn.yaml` の `cron.jobs` を上書きします。
7. CLI フラグ — 最後に、呼び出しごとに適用。

層 5・6 はスコープ付きで、それぞれのセクション（`mcp.servers` / `cron.jobs`）のみを持ち、セクション単位でマージされるため、無関係な設定には触れません。`${VAR}` interpolation は全 YAML 層マージ後に 1 回、CLI フラグの前に適用されます。

> **なぜ `.reyn/config/mcp.yaml` / `.reyn/config/cron.yaml` が勝つか**: これらは編集して再起動する静的ファイルと違い、ランタイム可変なレジストリ（`reyn mcp install` やランタイム cron 登録が書く）です。最後に置くことで、新規インストールした server / 登録した job が、operator が `reyn.yaml` も触らずに有効エントリになります。

`<project>/.reyn/config.yaml` はロードされません — これは廃止された汎用 config ファイルであり、上記の現役 `.reyn/config/mcp.yaml` / `.reyn/config/cron.yaml` レジストリとは別物です。ディスクに残っている場合、reyn は警告を出してスキップします。内容を `reyn.local.yaml` に移行して削除してください。

## `cost` ブロック

バジェット上限とレートリミット。すべてのフィールドは省略可能です。フィールドを省略（または `hard_limit` を `null` に設定）すると**無制限**になります。

```yaml
cost:
  # エージェントごとの上限（メモリ内、再起動または /budget reset でリセット）
  per_agent_tokens:
    hard_limit: 50000    # この数のトークンを超えると 1 エージェントが拒否される
    warn_ratio: 0.8      # hard_limit の 80% で警告（デフォルト: 0.8）
  per_agent_cost_usd:
    hard_limit: 2.00     # 1 エージェントが $2.00 消費した後に拒否

  # モデルごとのレートリミット（1 分あたりの呼び出し数）
  rate_limit_per_minute:
    openai/gpt-4o: 60
  rate_limit_warn_ratio: 0.8   # レートリミットの 80% で警告

  # 日次/月次クォータ（プロセス再起動をまたいで永続 — 午前 0 時 / 月初に自動リセット）
  # .reyn/state/budget_ledger.jsonl に保存。
  daily_tokens:
    hard_limit: 100000   # 今日 100k トークンを超えると拒否
    warn_ratio: 0.8
  daily_cost_usd:
    hard_limit: 5.00     # 今日 $5.00 を超えると拒否
  monthly_tokens:
    hard_limit: 1000000  # 今月 1M トークンを超えると拒否
  monthly_cost_usd:
    hard_limit: 50.00    # 今月 $50.00 を超えると拒否
```

| フィールド | スコープ | 永続 | リセット |
|---|---|---|---|
| `per_agent_tokens` | エージェントごと | メモリ内 | `/budget reset` または再起動 |
| `per_agent_cost_usd` | エージェントごと | メモリ内 | `/budget reset` または再起動 |
| `rate_limit_per_minute` | モデルごと | メモリ内（60 秒ウィンドウ） | 自動（スライディングウィンドウ） |
| `daily_tokens` | プロセスグローバル | 台帳ファイル | 午前 0 時（現地時間） |
| `daily_cost_usd` | プロセスグローバル | 台帳ファイル | 午前 0 時（現地時間） |
| `monthly_tokens` | プロセスグローバル | 台帳ファイル | 月初（現地時間） |
| `monthly_cost_usd` | プロセスグローバル | 台帳ファイル | 月初（現地時間） |

> **注意**: ルーター呼び出し上限（`max_router_calls_per_turn`）は `safety.loop` 配下にあります。上記の [`safety` ブロック](#safety-ブロック) を参照してください。

**上限の動作:** ハード上限を超えると、LLM の呼び出しが行われる前に拒否されます。現在の使用状況を見るには `/budget`、メモリ内カウンターをクリアするには `/budget reset` を使用します（日次/月次は reset の影響を受けません。永続台帳に基づいています）。

**台帳の場所:** `.reyn/state/budget_ledger.jsonl` — LLM 呼び出しごとに 1 レコード、fsync 付きの追記専用。このファイルは自動的にローテーションされません。月あたり数 MB 程度で成長し、必要に応じて手動でアーカイブできます。

## MCP サーバー {#mcp-servers}

reyn が [Model Context Protocol](../../concepts/tools-integrations/mcp.md) 経由で呼び出せる外部ツールサーバーです。`mcp.servers:` の各エントリは短い名前でキー付けされます（Skill が `permissions.mcp` で宣言し、`mcp` ops で発行するのと同じ名前）。

サーバーを追加する推奨方法は `reyn mcp install <server_id>`（[Reference: `reyn mcp`](../../reference/cli/mcp.md) 参照）です。エントリを自動的に書き込み、`~/.reyn/secrets.env` 経由で認証情報を処理します。手動設定も完全にサポートされています。

```yaml
mcp:
  servers:
    # stdio: ローカルプロセス、stdin/stdout 越しに JSON-RPC（大多数の公式サーバー）
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      env:
        FS_LOG_LEVEL: "info"

    # ~/.reyn/secrets.env からの認証情報を持つ stdio サーバー
    github:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-github"]
      env:
        GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_PERSONAL_ACCESS_TOKEN}

    # http: ホスト型サーバー、Streamable HTTP 越しの JSON-RPC
    internal_tools:
      type: http
      url: https://tools.example.internal/mcp
      headers:
        Authorization: "Bearer ${INTERNAL_TOOLS_TOKEN}"
```

| フィールド | 型 | 必須の対象 | 説明 |
|-------|------|--------------|-------------|
| `type` | string | すべて | `stdio` \| `http` \| `sse` |
| `command` | string | stdio | 起動する実行ファイル。 |
| `args` | list[string] | stdio（任意） | `command` に渡す引数ベクター。 |
| `env` | map[string,string] | stdio（任意） | 起動プロセスへの追加環境変数。値は `${VAR}` 展開に対応。 |
| `network` | bool | stdio（任意） | サンドボックス化されたサーバーがネットワークを使用できるか。`sandboxed_exec` と同じ single-source デフォルトに従う。ネットワークに到達すべきでないサーバーを隔離するには `false`。オペレーター所有 — モデルは設定不可。 |
| `subprocess` | bool | stdio（任意） | サンドボックス化されたサーバーが子プロセスを spawn（fork）できるか。デフォルト `true` — ほとんどの stdio サーバーは fork ベースの launcher（`npx` → node、`uvx` → tool）で起動し、起動に fork を要する。真に fork 不要なサーバーを hardening するには `false`。オペレーター所有 — モデルは設定不可。 |
| `write_paths` | list[string] | stdio（任意） | **サンドボックス化されたサーバーが書き込めるパス**。作業ディレクトリ（常に許可）に追加される。`~` は展開される。オペレーター所有 — モデルは設定不可。launcher はワークスペース外のユーザーごとのキャッシュに bootstrap するため、reyn は認識できる launcher に**デフォルト**のスコープを与える（`npx`/`npm` → `~/.npm`、`uvx`/`uv` → `~/.cache/uv` + `~/.local/share/uv`）。サーバーの runtime がそれ以外の場合、またはキャッシュを移動している場合（`XDG_CACHE_HOME`、`npm_config_cache` など）に設定する — デフォルトは標準の場所を前提としており、移動先を知り得ない。`write_paths` を宣言すると組み込みのデフォルトを**置き換える**ため、拡大だけでなく縮小もできる。サーバーが `Operation not permitted` / `EPERM` で起動に失敗した場合、エラーが拒否されたパスを示すので、そのパスをここで許可する。**スコープは狭く保つこと**: 書き込み許可はそのパスの*読み取り*も再開する。`~` を許可しても sensitive-read deny-list（`~/.ssh`、`~/.aws` 等）は無効化されない — 重なる write 許可より deny が勝つ（#2978）— が、サーフェスを無用に広げるため、ホームディレクトリではなく具体的なキャッシュディレクトリを許可すること。 |
| `url` | string | http, sse | エンドポイント URL。 |
| `headers` | map[string,string] | http, sse（任意） | 静的リクエストヘッダー。値は `${VAR}` 展開に対応。 |
| `call_timeout_seconds` | float | すべて（任意） | **すべての MCP op（list / call / probe）に対する end-to-end の上限**。既定 `120`、`<= 0` で無効化。server への接続と op の実行の両方を含む ∴ **起動（launch）も、この上限で打ち切られる**。特定 server が遅いと分かっている場合は上げ、速いと分かっていて fail-fast したい場合は下げる。per-call としては MCP SDK の `read_timeout_seconds` に渡され、`type: http` で `timeout` が設定する session レベルの既定を override する。 |
| `init_timeout` | float | すべて（任意） | **server が MCP handshake を完了するまで待つ上限**。既定 `60`、`0` でこの上限を無効化。handshake のみを縛り、tool call は縛らない ∴ 上げても遅い tool が timeout することはない。**起動したが黙っている** server（典型例: `command: uvx <pkg>` — 初回実行時に PyPI から取得する間、何も喋らない）はここで止まる。expire 時、reyn は原因の候補と対処を名指しするエラーを返す。既定が `call_timeout_seconds` の `120` より **下** なのは意図的: 両方が起動を覆うため、**先に発火した方が operator の読むメッセージを決める** — 汎用の per-op timeout は「なぜ起動が止まったか」を語れない。∴ 本当に遅い server に猶予が要るなら **両方を上げる**（`init_timeout` 単独では `call_timeout_seconds` を超える時間は買えない）。初回起動が遅いことへの恒久的な対処は、パッケージを事前 install して `command` を install 済みの実行ファイルに向けること — offline / proxy 越しでも起動できるようになるのも、この形。 |
| `auth` | string \| map | すべて（任意、`http` のみ） | サーバーごとの OAuth 2.1 設定。文字列 `"oauth"` または `{type: oauth, scopes?, client_id?, client_secret?}`。`http` トランスポート以外(`stdio`/`sse`)で指定するとエラー。詳細は [コンセプト: MCP § OAuth](../../concepts/tools-integrations/mcp.ja.md#oauth) 参照。 |
| `elicitation` | string | すべて（任意） | `prompt`(デフォルト) — サーバー起動の構造化入力要求(`elicitation/create`)がコンセントプロンプトとして表示される。`auto_decline` — そのようなすべての要求をプロンプトせずに decline する。[コンセプト: MCP § Elicitation](../../concepts/tools-integrations/mcp.ja.md#elicitation-サーバーからの構造化入力要求) 参照。 |
| `elicitation_timeout_seconds` | float | すべて（任意） | elicitation プロンプトに人間が回答するためのウォールクロック期限。デフォルト `120`。期限を過ぎた未回答の要求はキャンセルされます。 |

サーバーは設定ソースをまたいでマージされます: `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml`。マージは `mcp.servers` キーの shallow union です。マシンごとの `reyn.local.yaml` が残りを再宣言せずに単一サーバーを追加・上書きできます。

MCP ランタイムはコアインストールに同梱されます。各セッションの MCP クライアントは公式 `mcp` SDK の上に直接構築されます（#4283/#4298/#4299: クライアント経路から `fastmcp` は完全に退役）。この `mcp` SDK がコア依存なので extra は不要です。`fastmcp` は reyn 側のあらゆる依存宣言（コア・extra すべて）から削除されました（#4302: `tests/_support/` の MCP サーバー test-double を `mcp` 自身が同梱する `mcp.server.fastmcp` サーバーフレームワークへ移植済み）。ただしこれで全てではありません——同梱の `rag` プラグインは自分自身の `requirements.txt` に独自の `fastmcp>=3.4,<4` を宣言しています（register-only で、reyn 自身の `plugin_install` が読み込みも install もしません）——#4302 が見つけた実在の消費者です。この上限は #4388 で締められました——緩い `>=2.0`（未検証のメジャーバージョンまで許容）は #4371 の CI 調査では実際の原因ではなく赤herringでしたが、それ自体は独立した潜在リスクでした。プラグインのスクリプトを直接走らせる reyn の開発/CI ツール（実サブプロセスとして起動する sandbox ゲート、wheel-reachability smoke test 自身の MCP クライアント）は、この `requirements.txt` を自分自身のセットアップ手順としてインストールしており、`pyproject.toml` の宣言に fastmcp を再導入する形ではありません。`mcp` は `>=2.0,<3.0` にピン留めされています（#4412: 従来の `>=1.24,<2.0` から引き上げ——reyn 自身の MCP *サーバー*（`src/reyn/mcp/server.py`、mcp 2.0 が削除した `lowlevel.Server` のデコレータ API に依存していた）を、`_mcp_server_boundary.build_mcp_server` シーム経由で 2.0 のコンストラクタ kwarg 登録形式へ移植済み、#4368）。既存の `pip install -e ".[mcp]"` が解決し続けられるよう、空の `[mcp]` extra を後方互換エイリアスとして残しています。

[コンセプト: MCP](../../concepts/tools-integrations/mcp.md) でプロトコル概要を参照してください。

> **`mcp.search_threshold` は削除されました（#3218 / FP-0066 §7 P1a）。**
> `ReynConfig.mcp_search_threshold` フィールド + その `mcp.search_threshold:`
> パースは、確認済みの no-op として fold-remove されました：パースされた値は
> `build_tools()` のどちらの router_loop.py 呼び出しサイトにも一度も
> threading されていなかったため、設定しても効果がありませんでした。
> `build_tools()` 自身の `mcp_search_threshold` パラメータは引き続き存在します
> （デフォルト `0` — 常にインライン；`src/reyn/runtime/router_tools.py` の
> `MCP_SEARCH_THRESHOLD` 参照）が、`reyn.yaml` からは到達不能になりました —
> 非デフォルト動作が必要な呼び出し元はコードで明示的に渡します。基盤の
> `tool_search_tool` メカニズム自体の完全削除は FP-0033 で追跡されています。

## `skills` ブロック

`SKILL.md` ベースの skill を登録します — `mcp.servers` と同じ明示的登録モデルです(ディレクトリスキャンなし。skill が可視になるにはエントリが存在する必要があります)。

```yaml
skills:
  entries:
    pdf_editing:
      path: skills/pdf-editing/SKILL.md   # project-root 相対または絶対
      description: "PDF フォームのフィールドを入力・結合・抽出する"
      enabled: true
      visibility: menu                    # menu | on_demand | hidden
```

| フィールド | 型 | デフォルト | 説明 |
|-----------|-----|----------|------|
| `path` | string | 必須 | `SKILL.md`、またはそれを含むディレクトリへのパス。 |
| `description` | string | `""` | モデル向けの `## Skills` メニューに表示される一行サマリー(最初の行のみ、1024 文字上限 — [Agent Skills 仕様](https://agentskills.io/specification)が `description` に定める上限; #3550)。 |
| `enabled` | bool | `true` | `false` にするとエントリはレジストリから完全に除外されます。`visibility` より優先します。 |
| `visibility` | enum | `menu` | どの面が skill を名指すか: `menu`(`## Skills` システムプロンプトメニューに載る)\| `on_demand`(メニューには載らないが `skill_list` ツールが返す — 常駐トークンコストなし)\| `hidden`(どのモデル向け面にも現れない)。 |

`enabled: false` は `visibility` を参照する前にエントリを落とすため、2 つのフィールドが表すのは 6 状態ではなく 4 状態です。

**#2971 で削除: `auto_invoke`**(misnomer — skill を自動起動する機構は無く、メニュー描画だけを制御していた。当時メニューは skill を名指す唯一の面だったため、`false` は「広告しない」ではなく到達不能を意味した)。`auto_invoke` が残った config は load 時にエラーとなり置換先を提示します: `auto_invoke: true` → `visibility: menu`、`auto_invoke: false` → `visibility: hidden`。

`skills.entries` は `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml` ⊕ 動的な `<project>/.reyn/config/skills.yaml`(`skill_install_local` / `skill_install_source` chat ツールが書き込む)をまたいでマージされ、名前が衝突した場合は後の tier が優先します — `mcp.servers` と同じマージ形です。

登録モデル全体、3 層の露出モデル(メニュー / オンデマンド読み取り / バンドル資産)、インストールツールについては [コンセプト: Skills](../../concepts/tools-integrations/skills.md) を参照してください。

## `presentations` ブロック {#presentations-block}

`present` op 向けの**名前付きプレゼンテーションテンプレート**を登録します — `skills.entries` / `pipelines.entries` / `mcp.servers` と同じ明示登録モデルです。名前付きテンプレートの値は **blueprint** です: インライン `present` blueprint と同一の、宣言的で非実行なコンポーネントツリー(カタログコンポーネント + JSON-Pointer パスバインディング)。blueprint はエントリ内に**インライン**で存在し(ファイル間接参照なし — blueprint は小さな宣言的データです)、ロード時に構造的に検証されます。

名前付きテンプレートの登録は**operator/config アクション**です — インストールツールも、モデルが呼び出して登録できる op もありません。モデルは*インライン* blueprint のみを作成します。`present` op の `template:` による名前付き参照は、このレジストリに対する read-only な検索です。未知のテンプレート名はエラーではありません: `present` op はコンテンツタイプのデフォルトビューアを経由して汎用 YAML/text 表示にフォールバックするため、データは常にユーザーへ届きます。

```yaml
presentations:
  entries:
    search_results:
      blueprint:                              # 必須。インラインのコンポーネントツリー
        - component: table
          rows: {"$bind": "/results"}
          columns:
            - {header: Author, path: /author}
            - {header: Title,  path: /title}
      description: "Search results table"      # 任意
      enabled: true                            # 任意、デフォルト true
```

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `blueprint` | list または object | 必須 | 宣言的コンポーネントツリー(インライン `present` blueprint と同じ形状・カタログ)。ロード時に検証され、不正な blueprint はスキップされ(ログ記録)、hot-reload 時は reload 全体を拒否します(直近の正常な状態を保持)。 |
| `description` | string | `""` | 任意の一行サマリー。 |
| `enabled` | bool | `true` | `false` にするとエントリはレジストリから完全に除外されます。 |

`presentations.entries` は `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml` ⊕ 動的な `<project>/.reyn/config/presentations.yaml` をまたいでマージされ、名前が衝突した場合は後の tier が優先します — `skills.entries` / `pipelines.entries` / `mcp.servers` と同じマージ形です。`<project>/.reyn/config/presentations.yaml` 層はターン境界で hot-reload されるため、新しく登録されたテンプレートは再起動なしに次のターンで解決可能になります。

## `embedding` ブロック

RAG 埋め込みモデルクラスとバッチ設定。組み込みデフォルトが OpenAI パスをカバーしているため、`OPENAI_API_KEY` を設定した新規インストールでは `reyn.yaml` の変更は不要です。オフライン/エアギャップ環境を含む opt-in の全手順は [ガイド: semantic search を有効にする](../../guide/for-users/enable-semantic-search.ja.md) を参照。

```yaml
embedding:
  default_class: standard         # クラス未指定時に使用するクラス
  batch_size: 100                 # 埋め込み API 呼び出しごとのテキスト数（1–2048）
  max_concurrent_batches: 1       # 並列バッチ呼び出し数（1–10）
  max_retries: 3                  # 一時的エラーのリトライ数（0–10）
  retry_backoff: exponential      # exponential | linear
  tokenizer: cl100k_base          # チャンクサイズ推定用 tiktoken エンコーディング
  cost_warn_threshold: 10000      # 推定チャンク数がこれを超えると ask_user ゲートが起動
  classes:
    light:
      model: openai/text-embedding-3-small
    standard:
      model: openai/text-embedding-3-small
    strong:
      model: openai/text-embedding-3-large
    # 非デフォルト API エンドポイントを使用するカスタムクラス
    private:
      model: openai/text-embedding-3-small
      api_base: ${EMBEDDING_API_BASE}
```

### `embedding` フィールド

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `default_class` | 文字列 | `standard` | 埋め込み op でクラス未指定時に使用するクラス。`classes` のキーである必要があります。 |
| `batch_size` | int | `100` | 埋め込み API 呼び出しごとのテキスト数。有効範囲: 1–2048。 |
| `max_concurrent_batches` | int | `1` | 並列バッチ呼び出し数。有効範囲: 1–10。1 より大きい値は受け入れますが、並列パスが有効になるまで警告ログが出ます。 |
| `max_retries` | int | `3` | バッチ呼び出しごとの一時的エラーリトライ数。有効範囲: 0–10。 |
| `retry_backoff` | 文字列 | `exponential` | バックオフ戦略: `exponential` または `linear`。 |
| `tokenizer` | 文字列 | `cl100k_base` | チャンクサイズ推定に使用する tiktoken エンコーディング。 |
| `cost_warn_threshold` | int | `10000` | インデックス作成前に `ask_user` ゲートが起動する推定チャンク数の閾値。トップレベルの `cost_warn:` ブロック（モデル選択時の USD/1M トークン価格警告、EN 版参照）とは無関係 — `cost_warn` という名前の一致は偶然で、単位（チャンク数 vs USD）・発火契機（indexing vs モデル選択）・読み手（embedding パイプライン vs router）がすべて異なります。 |

### `embedding.classes` エントリ

`embedding.classes` の各キーはクラス名です。組み込みデフォルト（`light`、`standard`、`strong`）があらかじめ読み込まれ、ユーザーエントリで上書きや追加ができます。

| フィールド | 必須 | 説明 |
|-------|----------|-------------|
| `model` | はい | LiteLLM モデル文字列（例: `openai/text-embedding-3-small`）。 |
| `api_base` | いいえ | エンドポイント URL のオーバーライド。`${VAR}` interpolation に対応。 |
| `extra_body` | いいえ | API にそのまま渡すプロバイダー固有のペイロード。 |
| `extends` | いいえ | 同じ `classes` dict の別クラスから継承して特定フィールドをオーバーライド。 |

組み込みクラス（`classes:` が空または省略時に有効）:

| クラス | モデル | 備考 |
|-------|-------|-------|
| `light` | `openai/text-embedding-3-small` | `OPENAI_API_KEY` が必要。 |
| `standard` | `openai/text-embedding-3-small` | `OPENAI_API_KEY` が必要。 |
| `strong` | `openai/text-embedding-3-large` | `OPENAI_API_KEY` が必要。 |

3 つの組み込みクラスはすべて litellm 経由の OpenAI backed です。in-process のローカルバックエンドはありません（#3128 で `local-mini` / `local-e5` sentence-transformers クラスと `reyn[local-embed]` extras を削除済み）— ローカル / オフラインモデルが必要な operator は、自前で立てた litellm proxy 背後のモデルを指す custom `embedding.classes` エントリを追加します。セットアップ手順は [Concepts: RAG — Local and offline embedding models](../../concepts/data-retrieval/rag.md#local-and-offline-embedding-models)（英語）を参照。

## `chat` ブロック {#chat-compaction-block}

チャットは最初にコンテキストウィンドウを生のターンで充填し、履歴が
effective trigger（`component_weights` からモデルの実際のコンテキストウィンドウに対して
ウィンドウ相対で導出）を超えた時点で圧縮が発火します。Head・Tail ゾーンは
ターン数ではなく **トークンバジェット** で管理されます。

```yaml
chat:
  compaction:
    # バジェット配分: 整数の重み、起動時に正規化。
    # キー: head / body / tail / new_msg / compaction_batch
    component_weights:
      head:             10
      body:             5
      tail:             15
      new_msg:          10
      compaction_batch: 60
    section_caps_spec_tokens: 100
    use_chars4_estimate: false        # true = len(text)//4（レイテンシ opt-out）
    body_token_cap: 1500               # サマリー body トークン上限（post-truncation）
    resummarize_passes: 1              # hard_truncate 前の LLM 再圧縮パス数
    # body 内のセクション配分の重み、起動時に正規化。
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

### `chat.compaction` フィールド

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `component_weights` | map[str,int] | `{head:10, body:5, tail:15, new_msg:10, compaction_batch:60}` | 各プロンプトコンポーネントの整数の重み。起動時に `main_pool` に対して正規化。合計値は任意。 |
| `section_weights` | map[str,int] | （セクションごとのデフォルト） | body バジェット内のサブセクション配分の重み。`component_weights` と同じ shape セマンティクス。 |
| `section_caps_spec_tokens` | int | `100` | コンパクタープロンプト内の `section_token_caps` シリアライズ用静的オーバーヘッドバジェット。 |
| `body_token_cap` | int | `1500` | post-truncation 後のサマリー body トークン上限。 |
| `resummarize_passes` | int | `1` | `topic_arc` が body バジェットを超えた場合の最大 LLM 再圧縮パス数（`hard_truncate` floor 適用前）。`0` = 再圧縮なし（straight to floor）。 |
| `use_chars4_estimate` | bool | `false` | `true` の場合、`litellm.token_counter` の代わりに `len(text)//4` を使用（大規模デプロイ向けレイテンシ opt-out）。 |

### `chat.compaction.section_token_caps` フィールド

**強制されているのは `topic_arc` だけです。** 5つの値はすべてコンパクターのプロンプトへ
サイズの *目安* として渡されますが、`decisions` / `pending` / `session_user_facts` /
`artifacts_referenced` はLLMの応答をパースした値をそのまま使い、後処理での切り詰めは
一切ありません。`topic_arc` だけがLLM応答後に3段階の決定的な上限処理を通ります
（収まっていればそのまま → 超過していればLLMで再要約、`resummarize_passes` で回数制限 →
最後に決定的な `hard_truncate_summary` の床、#1163）。そのため `topic_arc` はbody
budgetを超えませんが、他の4つは超え得ます。

これは意図してそう設計された非対称ではなく、現状そうなっている、という事実です——
`topic_arc` だけが強制される理由を示す記録は見つかりませんでした。#1163が置き換えた
のは `topic_arc` の以前の単純な文字数カットで、他の4つはそもそも上限を持ったことが
ありません。塞ぐ予定もありません: `decisions`/`pending` 等が肥大しても、影響は最大1
ターンです。`router_loop_driver.py` の pre-frame guard（`context_budget_advisor.
maybe_force_compact`）が毎ターン送信前に有効トークンバジェットを再計算し、現在の
履歴がそれを超えていれば再度コンパクションを強制します——つまり4つの非強制セクションの
超過は次のターンで拾われて再圧縮されます。代償は「その1ターンだけ設定より大きい
セクションのまま走る」ことだけで、ハード失敗にも無制限膨張にもなりません。

| フィールド | デフォルト | 説明 |
|-------|---------|-------------|
| `topic_arc` | `200` | トピックアークサマリーセクションのトークン目標——5つのうちLLM応答後に強制されるのはこれだけ（上記参照）。 |
| `decisions` | `400` | 決定事項セクションのトークン目標。LLMへのプロンプト上の目安であり、返り値への強制はなし。 |
| `pending` | `400` | 保留項目セクションのトークン目標。LLMへのプロンプト上の目安であり、返り値への強制はなし。 |
| `session_user_facts` | `200` | 圧縮をまたいで引き継ぐユーザーファクトのトークン目標。LLMへのプロンプト上の目安であり、返り値への強制はなし。 |
| `artifacts_referenced` | `300` | アーティファクト参照一覧のトークン目標。LLMへのプロンプト上の目安であり、返り値への強制はなし。 |

### 廃止キー

`head_size`、`tail_size`、`trigger_total_tokens`、`min_compact_batch` は認識されなくなりました。
`reyn.yaml` に存在する場合、Reyn は起動時に `DeprecationWarning` を発行して無視します。
これらのキーを設定ファイルから削除してください — head/tail のサイズ管理は `component_weights`
によるトークンバジェットに移行し、自動圧縮はウィンドウ相対になりました。

## `audit_events` ブロック

チャットセッションイベントファイルの監査ログローテーションポリシー。Skill 実行イベントはラン 1 つにつき 1 ファイルを使用し、この設定の影響を受けません。

`events:` から改名（#4174 T5）— reyn における素の "event" は曖昧（audit-event /
WAL-event / hook-event のいずれか）。このブロックは常に audit-event のローテー
ションのみを扱っていた。

```yaml
audit_events:
  max_bytes: 10485760       # 10 MB でローテーション（デフォルト）
  max_age_seconds: 86400    # 1 日後にローテーション（デフォルト）
  cleanup_period_days: null # null = 自動削除なし（デフォルト）
```

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `max_bytes` | int | `10485760`（10 MB） | アクティブイベントファイルがこのサイズを超えるとローテーション。`0` = サイズベースのローテーションなし。 |
| `max_age_seconds` | int | `86400`（1 日） | アクティブイベントファイルがこの秒数を経過するとローテーション。`0` = 経過時間ベースのローテーションなし。 |
| `cleanup_period_days` | int \| null | `null` | クローズされたイベントファイルを `reyn events purge` が削除できるまでの保持期間（日）。`null` で自動削除を無効化。`0` は拒否されます — 無効化するには `null` を使用。 |

`max_bytes` と `max_age_seconds` の両方を `0` に設定するとローテーションを完全に無効化します。

## `voice` ブロック

**⚠️ 現在利用不可。** このブロックは今もparseされます(設定してもエラーにはなりません)が、consumerがありません — 旧 Textual TUI の Ctrl+R Whisper バインディング用に構築されたものですが、そのTUIは削除され inline CUI に置き換わりました(音声入力バインディングなし)。スキーマの完全性のためだけに残しています。[コンセプト: voice](../../concepts/tools-integrations/voice.md) を参照。

音声入力(Whisper)設定(consumerが存在する場合)。オプション機能 — `pip install 'reyn[voice]'`(`sounddevice` + `faster-whisper`)が必要です。ブロックは遅延ロードされるため、`[voice]` extra がない場合は録音キーが自動的に無効化されます。

```yaml
voice:
  enabled: true           # deps がインストールされていても Ctrl+R を無効化するには false
  model: small            # tiny | base | small | medium | large-v3
  language: ja            # ISO 639-1 コード; "" または null = 自動検出
  device: cpu             # cpu | cuda
  compute_type: int8      # int8 | float16 | float32
  sample_rate: 16000      # Whisper は 16 kHz モノラルを期待
  cpu_threads: 4          # 0 = OpenMP デフォルト
  num_workers: 1          # 並列転写ストリーム数
  max_duration_s: 300.0   # これ（秒）を超える録音は自動キャンセル
```

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `enabled` | bool | `true` | deps がインストールされていても Ctrl+R を完全に無効化するには `false`。 |
| `model` | 文字列 | `small` | Whisper モデルサイズ: `tiny` / `base` / `small` / `medium` / `large-v3`。 |
| `language` | 文字列 \| null | `ja` | ISO 639-1 言語コード。`""` または `null` で自動検出（短いクリップでは信頼性が低い）。 |
| `device` | 文字列 | `cpu` | 推論デバイス: `cpu` または `cuda`。`auto` は一部の Mac 環境で誤ったデバイスを選択するため非対応。 |
| `compute_type` | 文字列 | `int8` | 量子化精度: `int8` / `float16` / `float32`。 |
| `sample_rate` | int | `16000` | サンプルレート（Hz）。Whisper は 16 kHz モノラルを期待 — 変更しないでください。 |
| `cpu_threads` | int | `4` | faster-whisper の CPU スレッド数。`0` = OpenMP デフォルト。Apple Silicon での OpenMP/Python スレッドデッドロックを避けるため 4 に固定しています。 |
| `num_workers` | int | `1` | 並列転写ストリーム数。`1` でメモリとスレッド使用量を低く保ちます。 |
| `max_duration_s` | float | `300.0` | この秒数を超える録音を自動キャンセル。放置録音によるメモリ増大を防ぎます。 |

## `multimodal` ブロック

Reyn がバイナリメディア（`web_fetch` / `read_file` / MCP サーバー由来の画像）を扱う方法と、マルチモーダルアーティファクトのディスク上の保存先を制御します。

```yaml
multimodal:
  max_bytes: 5000000              # 5 MB — Anthropic の per-image API 上限
  on_oversize: ask                # ask | allow | deny
  media_dir: .reyn/media          # 画像バイナリのプロジェクト相対ディレクトリ
  tool_results_dir: .reyn/tool-results   # ツール結果ダンプのプロジェクト相対ディレクトリ
  base_url: null                  # クロスホスト path_ref 用のオプション正規 URL プレフィックス
```

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `max_bytes` | int | `5000000` (5 MB) | on-oversize ゲートが起動する前のデコード後ペイロードのバイト上限。バイナリサイズ (`len(response.content)` / `len(file_bytes)`) をカウント、base64 後の shape ではない。 |
| `on_oversize` | 文字列 | `ask` | メディアが `max_bytes` を超えた時の動作: `ask`（intervention bus でサイズ + ソース情報を提示してユーザーに確認、yes でロード、no でドロップ）、`allow`（無条件に受け入れ、信頼済み non-interactive パイプライン向け）、`deny`（無条件に拒否、op は `status="denied"` を返す。コスト重視コンテキスト向け）。 |
| `media_dir` | 文字列 | `.reyn/media` | 画像バイナリ保存のプロジェクト相対ディレクトリ。ファイルは timestamp + chain-id + tool prefix のフラット命名で `ls -la` が時系列ソートになる。operator が browse + delete 可能。 |
| `tool_results_dir` | 文字列 | `.reyn/tool-results` | テキスト系ツール結果ダンプのプロジェクト相対ディレクトリ。 |
| `base_url` | 文字列 \| null | `null` | クロスホスト `path_ref` 消費用のオプション正規 URL プレフィックス。`"https://reyn.example.com"`（= デプロイ済み `reyn web` の URL）等を設定すると、保存されるアーティファクトに `<base_url>/agents/<agent>/tool-results/<artifact>` を指す `url` フィールドが付与され、A2A peer / MCP client / ブラウザがリソースルーター経由で body を fetch 可能になる。未設定の場合は `url` フィールド非生成（same-host fast-path のみ）。 |

## `external_transports` ブロック

チャット向け受信トランスポート → MCP ツールルーティング。外部トランスポート名（Slack / LINE / Discord / ...）を、リプライを配信する MCP ツール + ルーター出力をツール引数に shape する `args_template` にマップします。

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

| フィールド | 型 | 説明 |
|-------|------|-------------|
| `transports.<name>.mcp_tool` | 文字列 | リプライを配信する完全修飾 MCP ツール名 (`<server>__<tool>`)。 |
| `transports.<name>.args_template` | マップ | MCP ツールに渡される shape。`${TRANSPORT_DEST}` はメッセージごとの宛先 ID（channel / user / room id）に解決、`${ROUTER_REPLY}` はルーターの最終テキストに解決。他の `${VAR}` 参照は標準 interpolation ルールに従って `os.environ` から解決。 |

トランスポートごとの contract と利用可能なテンプレート変数の全集合は `src/reyn/runtime/external_routing.py` を参照。

## 関連情報

- `reference/config/permissions.md` — 完全な Permission 文法
- `reference/config/state-dir.md` — `.reyn/` レイアウト
- [コンセプト: シークレット管理](../../concepts/runtime/secret-handling.md) — `~/.reyn/secrets.env` と `${VAR}` interpolation
- [Reference: `reyn secret`](../../reference/cli/secret.md) — CLI によるシークレット管理
- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — MCP サーバー管理 CLI
