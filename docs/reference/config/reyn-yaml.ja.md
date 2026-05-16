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
model: standard
models:
  light:    openai/gemini-2.5-flash-lite
  standard: openai/gpt-4o
  strong:   anthropic/claude-3-5-sonnet-20241022
```

## トップレベルキー

| キー | 型 | 説明 |
|-----|------|-------------|
| `model` | 文字列 | デフォルトのモデルクラス。`models` を通じて解決されます。`--model` でオーバーライド。 |
| `models` | マップ | クラス名 → LiteLLM モデル文字列 **または** dict（以下参照）。 |
| `output_language` | 文字列 | デフォルトの出力言語コード（例: `en`、`ja`）。`--output-language` でオーバーライド。 |
| `safety` | マップ | ランタイムの停止条件: ループ検出上限、タイムアウト、上限超過時ポリシー。以下参照。 |
| `cost` | マップ | バジェット上限とレート制限（エージェントごと、日次、月次）。以下参照。 |
| `plan` | マップ | プランモードのステップバジェットとリトライ設定。以下参照。 |
| `web` | マップ | `web_fetch` と MCP レジストリ呼び出しの SSL 設定。以下参照。 |
| `eval` | マップ | `reyn eval` のトレース exporter バックエンド。以下参照。 |
| `sandbox` | マップ | `sandboxed_exec` のバックエンド選択と非対応プラットフォームポリシー。以下参照。 |
| `action_retrieval` | マップ | FP-0034 ユニバーサルカタログの可視化 + 検索設定。以下参照。 |
| `embedding` | マップ | RAG 埋め込みモデルクラスとバッチ設定（ADR-0033）。以下参照。 |
| `chat` | マップ | チャットセッションの Head/Body/Tail 圧縮設定。以下参照。 |
| `voice` | マップ | チャット TUI の音声入力（Whisper）設定。以下参照。 |
| `events` | マップ | チャットセッションイベントファイルの監査ログローテーションポリシー。以下参照。 |
| `skill_search` | マップ | BM25 Skill 事前フィルター設定（FP-0024 Component A）。以下参照。 |
| `skill_resume` | マップ | 再起動時の曖昧なステップに対するレジューム ポリシー。以下参照。 |
| `self_improvement` | マップ | `skill_improver` の適用ゲートとバージョン上限（FP-0006）。以下参照。 |
| `mcp` | マップ | MCP サーバー定義と `search_threshold`。以下参照。 |
| `python` | マップ | Python preprocessor の追加許可モジュール。以下参照。 |
| `agent` | マップ | P6 イベント監査証跡と送信 HTTP ヘッダー用のエージェント識別子。以下参照。 |
| `auth` | マップ | `reyn auth login` 用の OAuth プロバイダー設定。以下参照。 |
| `cron` | マップ | スケジュール付きスキル実行 (FP-0009 Component B)。以下参照。 |
| `permissions` | マップ | デフォルトの Permission ポリシー。以下参照。 |
| `state_dir` | パス | Reyn がイベント、承認、Memory を書き込む場所。デフォルト `.reyn/`。 |
| `prompt_cache_enabled` | bool | システムプロンプトに Anthropic プロンプトキャッシュマーカーを付与。デフォルト `true`。 |
| `project_context_path` | 文字列 | すべての Phase システムプロンプトに注入する Markdown ファイル。デフォルト `REYN.md`。 |
| `api_base` | 文字列 | LiteLLM プロキシベース URL。通常は `reyn.local.yaml`（gitignored）に設定。 |

## `models` ブロック

`models:` の各エントリはクラス名を LiteLLM モデル文字列 **または** per-class LLM パラメータを宣言する dict にマップします。

### str 形式 — リテラル（後方互換）

str 値に **`/` が含まれる** 場合、LiteLLM モデル文字列として直接使用されます：

```yaml
models:
  light:    openai/gemini-2.5-flash-lite
  standard: openai/gpt-4o
  strong:   anthropic/claude-3-5-sonnet-20241022
```

str 形式を使用している既存の `reyn.yaml` はすべて変更なしで動作します。

### str 形式 — クラス参照省略形（新規）

str 値に **`/` が含まれない** 場合、`{extends: <name>}` の省略形として扱われます。
名前はフラット namespace（ユーザーエントリ + built-in カタログ）で解決されます：

```yaml
models:
  standard: claude-sonnet-thinking     # 等価: standard: {extends: claude-sonnet-thinking}
```

不明な省略形（ユーザーエントリにも built-in にも存在しない名前）は起動エラーになります。

### dict 形式 — plain kwargs

```yaml
models:
  standard: openai/gemini-2.5-flash-lite   # str 形式も dict エントリと併用可能

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
| `extends` | いいえ | 名前付きクラスから継承し、オーバーライドを deep merge（下記参照）。 |
| *（その他のフィールド）* | いいえ | litellm にそのまま渡されます（パススルーポリシー）。 |

> **コスト制限**: `max_tokens` ではなく `max_completion_tokens` を使用してください。`max_tokens` は多くのプロバイダーが無視するレガシーのソフトヒントです。`max_completion_tokens` は API レベルで強制されます（OpenAI o1+ および Anthropic モデル）。

**フィールドポリシー**: `model` のみ必須です。他のフィールドはすべてバリデーションなしで `litellm.acompletion` に直接渡されます（未知のフィールドも silent に転送されます — future-proof）。タイポは reyn エラーではなく silent な litellm 失敗を引き起こします。

**Skill / Phase 側オーバーライド**: サポートしていません。Operator config（`reyn.yaml`）が LLM パラメータの唯一の source of truth です。Skill 作者はクラス名のみを指定します（例：`model_class: strong`）。

**マージ順**: Reyn が管理する設定（`timeout`、`num_retries`、プロキシルーティング）は operator 宣言の kwargs より常に優先されます。

### dict 形式 — `extends` フィールド（新規）

`extends` を使用して別のクラスから継承し、特定のフィールドをオーバーライドできます。
参照される名前はフラット namespace（ユーザーエントリ + built-in カタログ）で解決されます。

```yaml
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

**不明な参照**: namespace（ユーザーエントリまたは built-in カタログ）に存在しない名前の参照は起動エラーになります。

### Built-in カタログ

Reyn には、namespace にプリロードされた一般的なモデルクラスの built-in カタログが付属しています。
`reyn.yaml` に宣言せずに名前で参照できます：

| クラス名 | プロバイダー / モデル | 備考 |
|---|---|---|
| `claude-sonnet` | `anthropic/claude-3-7-sonnet` | |
| `claude-sonnet-thinking` | `anthropic/claude-3-7-sonnet` + thinking 有効 | budget_tokens=8000 |
| `claude-haiku` | `anthropic/claude-3-5-haiku` | |
| `gpt-4o-mini` | `openai/gpt-4o-mini` | |
| `gpt-4o` | `openai/gpt-4o` | |
| `gemini-flash-lite` | `openai/gemini-2.5-flash-lite` | |
| `gemini-3.1-flash-preview` | `openai/gemini-3.1-flash-preview` | |
| `gemini-2.0-flash` | `openai/gemini-2.0-flash` | `thinking_budget=0` で thinking 無効化 |

ユーザー宣言エントリは同名の built-in を**上書き**します。built-in カタログは便利な出発点であり、`reyn.yaml` が常に source of truth です。

詳細は [Reference: built-in models](../builtin-models.md) を参照してください。

## `safety` ブロック

停止条件の統合ネームスペース。各値は対応する CLI フラグで呼び出しごとにオーバーライドできます。（旧 `limits:` キーは FP-0004/0005 で削除されました。`safety:` が唯一の信頼できる情報源です。）

```yaml
safety:
  loop:
    max_phase_visits: 25         # ランごとの Phase あたりの上限; 0 = 無制限 (--max-phase-visits)
    max_act_turns_per_phase: 10  # Phase 訪問内の LLM ↔ op ラリー数; 0 = 無制限
    max_router_calls_per_turn: 3 # ユーザーターンごとのチャットルーター呼び出し数
    max_agent_hops: 3            # 最大委譲深度
  timeout:
    llm_call_seconds: 60         # 呼び出しごとの HTTP タイムアウト (--llm-timeout)
    llm_max_retries: 3           # 呼び出しごとの一時的エラーのリトライ数 (--llm-max-retries)
    phase_seconds: 0             # Phase ごとのウォールクロックバジェット; 0 = 無制限 (--phase-budget)
    chain_seconds: 60            # デリゲート返答待機時間
  on_limit:
    mode: unattended             # interactive | unattended | auto_extend
    auto_extend_times: 1         # （auto_extend モード）自動延長回数
    ask_timeout_seconds: 60      # （interactive モード）ユーザープロンプトのタイムアウト
```

### `safety.loop` フィールド

| パス | 型 | デフォルト | CLI フラグ | 説明 |
|------|------|---------|---------|-------------|
| `safety.loop.max_phase_visits` | int | `25` | `--max-phase-visits` | ランごとの任意の単一 Phase への再訪問上限。`0` = 無制限。 |
| `safety.loop.max_act_turns_per_phase` | int | `10` | — | 1 回の Phase 訪問内で許可される LLM ↔ op ラリー数。`0` = 無制限。 |
| `safety.loop.max_router_calls_per_turn` | int | `3` | — | ユーザーターンごとのチャットルーター呼び出し数。`0` = 無制限。 |
| `safety.loop.max_agent_hops` | int | `3` | — | 最大委譲深度（ユーザー → A → B → C = 3 ホップ）。 |

### `safety.timeout` フィールド

| パス | 型 | デフォルト | CLI フラグ | 説明 |
|------|------|---------|---------|-------------|
| `safety.timeout.llm_call_seconds` | float（秒） | `60` | `--llm-timeout` | LiteLLM に渡される呼び出しごとの HTTP タイムアウト。 |
| `safety.timeout.llm_max_retries` | int | `3` | `--llm-max-retries` | LLM 呼び出しごとの一時的エラーのリトライ数（LiteLLM 指数バックオフ）。 |
| `safety.timeout.phase_seconds` | float（秒） | `0` | `--phase-budget` | Phase ごとのウォールクロックバジェット。リトライ/ターンの境界でのソフトチェック。呼び出し途中はキャンセルしない。`0` = 無制限。 |
| `safety.timeout.chain_seconds` | float（秒） | `60` | — | マルチエージェントチェーンがデリゲート返答を待機する時間。上限超過後にランタイムが上流エラーを生成。`0` = 無効。 |

### `safety.on_limit` フィールド

| パス | 型 | デフォルト | 説明 |
|------|------|---------|-------------|
| `safety.on_limit.mode` | 文字列 | `unattended` | ループ/タイムアウト上限発動時の動作。`interactive` — `ask_user` でユーザーに延長許可を確認。`unattended` — 即時中止（`reyn run` / CI のデフォルト）。`auto_extend` — `auto_extend_times` 回自動延長後に中止。 |
| `safety.on_limit.auto_extend_times` | int | `1` | `unattended` フォールスルーまでの自動延長回数。`mode: auto_extend` 時のみ使用。 |
| `safety.on_limit.ask_timeout_seconds` | float（秒） | `60` | `interactive` モードでユーザー返答を待機する時間。タイムアウト時は拒否として扱われ、partial data で中止。 |

## `plan` ブロック

プランのステップ実行バジェットとリトライ動作を制御します。

```yaml
plan:
  step_max_iterations: 5   # ステップあたりの最大 RouterLoop ターン数（デフォルト: 5）
  retry_limit: 3           # ステップ失敗時の最大自動リトライ数（デフォルト: 3）
```

| キー | 型 | デフォルト | 説明 |
|-----|------|---------|-------------|
| `step_max_iterations` | integer | `5` | 1 つのプランステップが失敗として記録される前に消費できる最大 RouterLoop イテレーション数。 |
| `retry_limit` | integer | `3` | 一時的エラーによるステップあたりの最大自動リトライ数。上限到達後はユーザーにバジェット延長を求めます。トークン制限と同様のコスト保護上限として機能します。 |

## `web` ブロック

`web_fetch` と MCP パッケージレジストリの SSL 設定（FP-0022）。

```yaml
web:
  fetch:
    verify_ssl: true     # true | false | 省略（デフォルト: 環境変数チェーン）
    ca_bundle: /path/to/ca-bundle.pem   # 省略可; カスタム CA バンドル
```

優先度チェーン（高い順）:

| 優先度 | 条件 | 有効な SSL 設定 |
|--------|------|----------------|
| 1 | `web.fetch.ca_bundle` 設定あり | カスタム CA バンドルファイル（`verify=<path>`） |
| 2 | `web.fetch.verify_ssl: false` | SSL 検証を無効化（`verify=False`）— **管理された環境のみ** |
| 3 | `web.fetch.verify_ssl: true` | SSL 検証を強制（`verify=True`） |
| 4 | 両方省略 | フォールスルー: `SSL_VERIFY` 環境変数 → `litellm.ssl_verify` → `SSL_CERT_FILE` → `True` |

`verify_ssl` と `ca_bundle` は MCP レジストリの HTTP 呼び出し（パッケージインストール）にも適用されます。

## `eval` ブロック

トレース exporter バックエンド。設定すると、すべての Skill 実行の P6 イベントトレースを指定バックエンドに送出します（FP-0007）。

```yaml
eval:
  exporters:
    - type: file
      path: .reyn/traces/        # exporter 未設定時のデフォルト
    - type: langfuse
      public_key: ${LANGFUSE_PUBLIC_KEY}
      secret_key: ${LANGFUSE_SECRET_KEY}
      host: https://cloud.langfuse.com   # 省略可; デフォルトはクラウドエンドポイント
    - type: otlp
      endpoint: http://localhost:4317
    - type: ietf_audit
      path: .reyn/audit/         # IETF Agent Audit Trail ドラフト形式
```

| `type` | 説明 |
|--------|------|
| `file` | `path` 以下の JSON-lines ファイル。`exporters` が空のときのデフォルトバックエンド。 |
| `langfuse` | Langfuse インスタンスにトレースを送信。`public_key` / `secret_key` は `${VAR}` 環境変数補間をサポート。 |
| `otlp` | OpenTelemetry Protocol。`endpoint` は OTLP gRPC または HTTP レシーバー。 |
| `ietf_audit` | IETF Agent Audit Trail ドラフト形式で `path` に書き込み。 |

すべての exporter は fire-and-forget です: エクスポートの失敗はログに記録されますが、Skill 実行を中止しません。

## `sandbox` ブロック

`sandboxed_exec` op のバックエンド選択と非対応プラットフォームポリシー（FP-0017）。

```yaml
sandbox:
  backend: auto          # auto | seatbelt | landlock | noop
  on_unsupported: warn   # warn | error | ignore
```

| キー | 型 | デフォルト | 説明 |
|-----|------|---------|-------------|
| `backend` | 文字列 | `auto` | 強制バックエンド。`auto` は OS が選択: macOS < 26 → `seatbelt`（sandbox-exec SBPL）、Linux ≥ 5.13 かつ `sandbox-linux` extra インストール済み → `landlock`（+ オプションの seccomp-BPF）、その他 → `noop`（監査のみ、強制なし）。明示的な値で特定バックエンドを強制できます。 |
| `on_unsupported` | 文字列 | `warn` | 要求バックエンドがこのプラットフォームで利用不可の場合のポリシー。`warn` は WARNING をログに記録して `noop` にフォールバック。`error` は `RuntimeError` を発生（強制が必須な本番環境のフェイルファスト）。`ignore` はサイレントにフォールバック。 |

[リファレンス: control-ir — `sandboxed_exec`](../runtime/control-ir.md#sandboxed_exec) で op スキーマとバックエンド選択の詳細を参照してください。

## `action_retrieval` ブロック

FP-0034 ユニバーサルカタログの可視化 + 検索設定。 チャット Router に **ユニバーサル wrapper** (`list_actions` / `describe_action` / `invoke_action`) を提供し、 全 skill / agent / MCP / file / memory / RAG カテゴリで統一の browse / describe / invoke を実現する。 PR-3b-iv 以降デフォルト ON — 既存の tools= shape を保持したい operator は `universal_wrappers_enabled: false` でオプトアウト可能。

```yaml
action_retrieval:
  universal_wrappers_enabled: true    # PR-3b-iv 以降デフォルト; false でオプトアウト
  embedding_class: null               # search_actions 用の embedding.classes 名
  hot_list_n: 10                      # Phase 2 — top-N freq+recency 投影
  mode: default                       # default | minimal | performance (§D24)
```

> **Phase 6 cleanup (2026-05-16)**: `hide_legacy_tools` flag は完全削除。
> wrapper-only path が production 既定 (= 4 universal wrappers + hot
> list aliases、 legacy per-kind tool は `tools=` に出ない)。 dogfood
> batch 26 N=5 で validated (= 32/35 = 91.4% verified、 Brier 0.177、
> hallucination 0/35)。 legacy handler は wrapper の backing
> implementation として registry 残存 (= `invoke_action` が
> `universal_dispatch.py` 経由で dispatch)。

### `action_retrieval` フィールド

| フィールド | 型 | デフォルト | 説明 |
|-----|------|---------|-------------|
| `universal_wrappers_enabled` | bool | `true` | `true` (PR-3b-iv 以降デフォルト) の時、 Router の `tools=` は 4 universal wrappers (`list_actions` / `search_actions` / `describe_action` / `invoke_action`) + hot list direct aliases のみ。 legacy per-kind tool (`invoke_skill` / `call_mcp_tool` 等) は LLM に surface されず、 wrapper の backing handler として registry に残存。 `search_actions` は `embedding_class` で別途ゲート (FP-0034 §D14)。 `false` 設定で wrapper surface 自体を無効化 (= catalog routing なし、 legacy のみ — fixture-stability test 用)。 |
| `embedding_class` | string \| null | `null` | action-retrieval の semantic 検索 (FP-0034 §D13) に使用する [`embedding.classes`](../../concepts/rag.md) のエントリ名。 `null` または空の場合、 wrapper が有効でも `search_actions` は `tools=` から除外される。 設定すると cold-start session で [eager embedding build](#reyn-chat---eager-embedding-build) を発動し Turn-1 hallucination を回避。 |
| `hot_list_n` | int | `10` | top-N `freq+recency` direct alias のホットリスト投影サイズ (FP-0034 §D2 / §D24)。 `0` 以上必須。 `0` で完全オプトアウト (= §D24 minimal モード)。 |
| `mode` | string | `"default"` | §D24 の運用モードラベル: `"minimal"` (キャッシュ安定性最大、 ホットリストなし) / `"default"` (バランス) / `"performance"` (大規模ホットリスト)。 自由文字列で、 呼び出し側がセマンティクスを上乗せ。 |

### クイックスタート — オプトアウト

```yaml
# reyn.yaml — FP-0034 以前の tools= shape を保持
action_retrieval:
  universal_wrappers_enabled: false
```

再起動後、 チャット Router の `tools=` 末尾に 3 wrapper が含まれる (有効時 — デフォルト)。 LLM は以下を呼び出し可能:

- `list_actions(category=["skill"])` → qualified name 形式 (例: `skill__code_review`) で利用可能 skill を列挙
- `describe_action(action_name="skill__code_review")` → input schema を取得
- `invoke_action(action_name="skill__code_review", args={...})` → 既存 handler 経由で実行

リソースカテゴリ (`mcp.server`, `rag.corpus`, `memory.entry`, …) も canonical default semantic で `invoke_action` をサポート (FP-0034 §D19)。

不明な action 名は文字列類似度でランクされた `suggestions` を含む構造化エラー応答を返し、 LLM は 1 turn で復帰可能 (FP-0034 §D12)。

### 互換性ノート

PR-3b-iv 以降デフォルト `true`。 テストスイートはフリップに対し構造的に絶縁されている (= LLMReplay テストは新 accessor を実装しない `FakeRouterHost` を使用 → `getattr` フォールバックで False → 録画済 fixture は引き続き有効)。 フリップは production runtime の tools= shape のみに影響。 operator は `universal_wrappers_enabled: false` でオプトアウトし FP-0034 以前のバイト単位互換のチャット挙動を保持できる。

後続 FP-0034 フェーズ (§D9 カテゴリのみ表示への system prompt リファクタリング、 embedding-driven hot list と `search_actions` 有効化、 冗長 tool の削減) は別リリースでランディング。 各 dogfood 検証で確認するまでオプトイン継続。

ツールレジストリ / dispatch の背景は [`docs/concepts/architecture.md`](../../concepts/architecture.md) を参照。

## `agent` ブロック

監査証跡と HTTP ヘッダー伝播のためのランタイムエージェント識別子（FP-0016 Component E）。

```yaml
agent:
  id: "reyn/acme/code-review-agent"  # デフォルト: reyn/<hostname>
```

### `agent` フィールド

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `agent.id` | 文字列 | `reyn/<hostname>` | この Reyn インスタンスの安定識別子。すべての P6 イベントペイロードに `agent_id` としてスタンプされ、MCP / A2A / 外部 HTTP リクエストの送信時に `X-Reyn-Agent-Id` ヘッダーとして付与される（SOC2 / ISO27001 / METI v1.1 監査パターン）。推奨フォーマット: `reyn/<org>/<role>`（operator 定義）。空文字列を指定した場合はデフォルトにフォールバックし、空の `agent_id` がイベントやヘッダーに漏れるのを防ぐ。 |

デフォルト `reyn/<hostname>` により、フレッシュインストールでも operator の設定なしに使用可能な識別子が付与されます。マルチエージェントフリートや安定したロール単位の識別子が必要なエンタープライズデプロイでは `reyn.yaml` でオーバーライドしてください。

[コンセプト: マルチエージェント — Agent ID 伝播](../../concepts/multi-agent.md) でクロスエージェントトレースと A2A ヘッダー転送の詳細を参照してください。

## `auth` ブロック

`reyn auth login` 用の OAuth プロバイダー設定（FP-0016 Component C）。`auth.providers` 以下の各名前付きエントリが RFC 8628 Device Authorization Grant プロバイダーを定義します。デフォルトは空であり、operator が認証対象のプロバイダーを宣言します。

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

`${secret:<key>}` の値はコンフィグロード時に `~/.reyn/secrets.env` から解決されます（ADR-0030）。保存には `reyn secret set <key>` を使用します。

関連情報:

- [Reference: `reyn auth`](../../reference/cli/auth.md) — `reyn auth login/list/revoke` コマンド
- [コンセプト: シークレット管理](../../concepts/secret-handling.md) — OAuth ライフサイクルと認証情報スコープ
- [コンセプト: マルチエージェント](../../concepts/multi-agent.md) — エージェント識別子伝播

## `cron:` ブロック (FP-0009 Component B)

定期的なスキル実行をスケジュールします。スケジューラーは `reyn web` の一部（= FastAPI lifespan で起動）として、または `reyn cron run` 経由のフォアグラウンドプロセスとして実行されます。

```yaml
cron:
  jobs:
    - name: index_events_hourly
      skill: index_events
      schedule: "0 */6 * * *"   # 6時間ごと
      input: {}
      enabled: true

    - name: weekly_ops_report
      skill: ops_report
      schedule: "0 9 * * MON"   # 月曜 09:00
      input:
        since_days: 7
      enabled: true
```

### フィールド

- **`name`** (必須) — ジョブ識別子。スケジュール内で一意である必要があります
- **`skill`** (必須) — 呼び出す stdlib またはプロジェクトスキルの名前
- **`schedule`** (必須) — 5 フィールドの cron 式
  （分 / 時 / 日 / 月 / 曜日）
- **`input`** (省略可、デフォルト `{}`) — スキルに渡す入力アーティファクト
- **`enabled`** (省略可、デフォルト `true`) — `false` にすると設定にエントリを保持したままスケジューリングをスキップします

### 関連情報

- `docs/reference/cli/cron.md` — `reyn cron run/list/status`
- `docs/concepts/operational-intelligence.md` — `index_events` /
  `ops_report` のユースケース

## `permissions` ブロック

プロジェクト全体のケイパビリティデフォルト。`skill.md` の Skill ごとの Permission がこれらをオーバーライドします。

```yaml
permissions:
  shell: deny           # deny | ask | allow
  file:
    read:  [".reyn/", "src/stdlib/"]
    write: [".reyn/state/", "reyn/local/"]
  python:
    safe:    allow      # safe モードの python ステップのデフォルト
    unsafe:  deny       # unsafe モードには --allow-unsafe-python も必要
    allowed_modules:
      - math
      - statistics
      - json
      - re
  mcp_install: ask      # deny | ask | allow （デフォルト: ask）
```

### `permissions.mcp_install`

`reyn mcp install` または `mcp_install` Control IR op 経由で MCP サーバーを設定に追加できるかを制御します。3 つの値：

| 値 | 動作 |
|-------|-----------|
| `ask`（デフォルト） | サーバーごとの初回インストール時にインタラクティブプロンプト。承認は `mcp_install:<server_id>` キーで `.reyn/approvals.yaml` に永続化されます。 |
| `allow` | プロンプトなしでインストール。プライベートレジストリと組み合わせて「承認済みサーバーのみ」ポリシーを実現する際に有用。 |
| `deny` | すべてのインストール試行を拒否。サーバーリストを一元管理するチーム設定のプロジェクトスコープ `reyn.yaml` に適切。 |

この設定は標準のスコープ層マージに参加します。プロジェクトスコープの `reyn.yaml` で `deny` を設定し、個々の開発者が `reyn.local.yaml` で `mcp_install: ask` にオーバーライドすることができます。

エンタープライズパターン — プライベートレジストリへのインストールを制限：

```yaml
# reyn.yaml（プロジェクトスコープ — git にコミット）
mcp:
  registries:
    - https://mcp-registry.internal.acme.com/    # プライベートレジストリを先頭に
    - https://registry.modelcontextprotocol.io/   # パブリックフォールバック
permissions:
  mcp_install: allow    # チームはインストール可能だが、上記レジストリのサーバーのみ事実上限定
```

スコープ層との詳細な連動とエンタープライズユースケースは [コンセプト: パーミッションモデル](../../concepts/permission-model.md#mcp_install-パーミッション) を参照してください。

完全な Permission 文法は `reference/config/permissions.md` に記載されています。

## `${VAR}` interpolation {#var-interpolation}

`reyn.yaml`（または `reyn.local.yaml` / `~/.reyn/config.yaml`）の任意のセクションの任意の文字列フィールドで、`${VAR}` 構文を使って環境変数を参照できます。変数は起動時、`~/.reyn/secrets.env` を環境変数にロードした後に `os.environ` から解決されます（詳細は [コンセプト: シークレット管理](../../concepts/secret-handling.md) 参照）。

```yaml
# reyn.yaml — ${VAR} はすべての文字列フィールドで使用可能
models:
  default-sonnet:
    model: claude-sonnet-4-5
    api_key: ${ANTHROPIC_API_KEY}          # LLM API キー — secrets.env またはシェルから解決
    extra_body:
      headers:
        Authorization: ${LITELLM_PROXY_TOKEN}

litellm:
  api_base: ${LITELLM_API_BASE}            # LiteLLM プロキシ URL

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

## プロキシ / `api_base`

モデルをローカルの LiteLLM プロキシ経由でルーティングする場合は、URL を `reyn.yaml` ではなく `reyn.local.yaml`（gitignored）に書きます。環境変数の参照も使えます：

```yaml
# reyn.local.yaml
api_base: ${LITELLM_API_BASE}    # または直接書く: http://localhost:4000
```

## 解決順序

各設定について、Reyn は（優先度が低い方から）マージします:

1. `~/.reyn/config.yaml`（ユーザーグローバル）
2. `reyn.yaml`（プロジェクト）
3. `reyn.local.yaml`（プロジェクト、gitignored）
4. CLI フラグ

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

> **注意**: チェーンごとの Skill スポーン・トークン上限（`skill_calls_per_chain`、`skill_tokens_per_chain`）とルーター呼び出し上限（`max_router_calls_per_turn`）は FP-0004/0005 で `safety.loop` に移動しました。上記の [`safety` ブロック](#safety-ブロック) を参照してください。

**上限の動作:** ハード上限を超えると、LLM の呼び出しが行われる前に拒否されます。現在の使用状況を見るには `/budget`、メモリ内カウンターをクリアするには `/budget reset` を使用します（日次/月次は reset の影響を受けません。永続台帳に基づいています）。

**台帳の場所:** `.reyn/state/budget_ledger.jsonl` — LLM 呼び出しごとに 1 レコード、fsync 付きの追記専用。このファイルは自動的にローテーションされません。月あたり数 MB 程度で成長し、必要に応じて手動でアーカイブできます。

## MCP サーバー {#mcp-servers}

reyn が [Model Context Protocol](../../concepts/mcp.md) 経由で呼び出せる外部ツールサーバーです。`mcp.servers:` の各エントリは短い名前でキー付けされます（Skill が `permissions.mcp` で宣言し、`mcp` ops で発行するのと同じ名前）。

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
| `url` | string | http, sse | エンドポイント URL。 |
| `headers` | map[string,string] | http, sse（任意） | 静的リクエストヘッダー。値は `${VAR}` 展開に対応。 |

サーバーは設定ソースをまたいでマージされます: `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml`。マージは `mcp.servers` キーの shallow union です。マシンごとの `reyn.local.yaml` が残りを再宣言せずに単一サーバーを追加・上書きできます。

MCP ランタイムはオプション依存です。公式の `mcp` Python SDK を取り込むには `pip install -e ".[mcp]"` でインストールします。

### `mcp.search_threshold`

すべての接続済みサーバーにわたる MCP ツールの総数がこの閾値に達すると、`build_tools()` が全 MCP ツールスキーマのインライン展開から Anthropic の `tool_search_tool`（遅延ロードモード）に切り替わります。デフォルト `30`。`0` で無効化。

```yaml
mcp:
  search_threshold: 30   # デフォルト; スキーマを常にインライン化するには 0 に設定
  servers:
    ...
```

[コンセプト: MCP](../../concepts/mcp.md) でプロトコル概要、[How-to: MCP サーバーを使う](../../guide/for-skill-authors/use-an-mcp-server.md) でエンドツーエンドのクイックスタートを参照してください。

## `embedding` ブロック

RAG 埋め込みモデルクラスとバッチ設定（ADR-0033）。組み込みデフォルトが OpenAI パスをカバーしているため、`OPENAI_API_KEY` を設定した新規インストールでは `reyn.yaml` の変更は不要です。

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
| `cost_warn_threshold` | int | `10000` | インデックス作成前に `ask_user` ゲートが起動する推定チャンク数の閾値。 |

### `embedding.classes` エントリ

`embedding.classes` の各キーはクラス名です。組み込みデフォルト（`light`、`standard`、`strong`）があらかじめ読み込まれ、ユーザーエントリで上書きや追加ができます。

| フィールド | 必須 | 説明 |
|-------|----------|-------------|
| `model` | はい | LiteLLM モデル文字列（例: `openai/text-embedding-3-small`）。 |
| `api_base` | いいえ | エンドポイント URL のオーバーライド。`${VAR}` interpolation に対応。 |
| `extra_body` | いいえ | API にそのまま渡すプロバイダー固有のペイロード。 |
| `extends` | いいえ | 同じ `classes` dict の別クラスから継承して特定フィールドをオーバーライド。 |

組み込みクラス（`classes:` が空または省略時に有効）:

| クラス | モデル |
|-------|-------|
| `light` | `openai/text-embedding-3-small` |
| `standard` | `openai/text-embedding-3-small` |
| `strong` | `openai/text-embedding-3-large` |

## `chat` ブロック

チャットセッションの圧縮設定 — 最近のターンを失わずにコンテキストを簡潔に保つ Head/Body/Tail トークンバジェット。

```yaml
chat:
  compaction:
    trigger_total_tokens: 30000   # カバーされない中間部がこれを超えると圧縮
    head_size: 12                  # 最初の N ユーザー/エージェントターンを生のまま保持
    tail_size: 12                  # 最後の N ユーザー/エージェントターンを生のまま保持
    body_token_cap: 1500           # 全 body サマリーセクションの合計トークン上限
    min_compact_batch: 5           # N ターン未満の圧縮はスキップ
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
| `trigger_total_tokens` | int | `30000` | 会話のカバーされない中間部がこのトークン数を超えると圧縮を実行。 |
| `head_size` | int | `12` | 生のまま保持する最初のユーザー/エージェントターン数（要約対象外）。 |
| `tail_size` | int | `12` | 生のまま保持する最新のユーザー/エージェントターン数。 |
| `body_token_cap` | int | `1500` | 全 body サマリーセクション合計のトークンバジェット。 |
| `min_compact_batch` | int | `5` | 吸収するターン数がこれ未満の場合は圧縮をスキップ（小さな圧縮を回避）。 |

### `chat.compaction.section_token_caps` フィールド

| フィールド | デフォルト | 説明 |
|-------|---------|-------------|
| `topic_arc` | `200` | トピックアークサマリーセクションのトークン上限。 |
| `decisions` | `400` | 決定事項セクションのトークン上限。 |
| `pending` | `400` | 保留項目セクションのトークン上限。 |
| `session_user_facts` | `200` | 圧縮をまたいで引き継ぐユーザーファクトのトークン上限。 |
| `artifacts_referenced` | `300` | アーティファクト参照一覧のトークン上限。 |

## `events` ブロック

チャットセッションイベントファイルの監査ログローテーションポリシー（PR20）。Skill 実行イベントはラン 1 つにつき 1 ファイルを使用し、この設定の影響を受けません。

```yaml
events:
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

チャット TUI の音声入力（Whisper）設定（Ctrl+R で録音）。オプション機能 — `pip install 'reyn[voice]'`（`sounddevice` + `faster-whisper`）が必要です。ブロックは遅延ロードされるため、`[voice]` extra がない場合は録音キーが自動的に無効化されます。

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

## `skill_search` ブロック

BM25 Skill 事前フィルター設定（FP-0024 Component A）。カタログが `threshold` を超える Skill 数になると、ルーターは `tools=` を構築する前に上位 `top_k` の BM25 キーワードマッチに利用可能 Skill の列挙を絞り込みます。BM25 が 0 件を返した場合は全列挙にフォールバック — Skill が見えなくなることはありません。

```yaml
skill_search:
  threshold: 20    # BM25 が有効化されるカタログサイズ; 0 = 常にフィルター
  top_k: 5         # BM25 が返す Skill 数
  backend: bm25    # bm25（デフォルト）; embedding / hybrid は将来のフェーズ向けに予約
```

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `threshold` | int | `20` | BM25 事前フィルタリングが有効化されるカタログサイズ。`0` で常にフィルタリング; 大きな数値で実質的に無効化。 |
| `top_k` | int | `5` | BM25 が返す最良マッチ Skill 数。最小値 `1`。 |
| `backend` | 文字列 | `bm25` | 検索バックエンド。`bm25` が唯一のアクティブバックエンド。`embedding` と `hybrid` は将来のフェーズ向けに予約。 |

## `skill_resume` ブロック

ステップ途中で中断された Skill 実行のレジューム ポリシー。*曖昧なステップ* とは `step_started` WAL イベントに対応する `step_completed` / `step_failed` がないもので、op が外部で確定している可能性があります。

```yaml
skill_resume:
  default: retry            # retry | skip | discard_skill | prompt
  per_skill:
    my_idempotent_skill: retry
    my_side_effect_skill: discard_skill
```

| ポリシー | 説明 |
|--------|-------------|
| `retry`（デフォルト） | 曖昧なステップを再実行。読み取り専用 op や冪等性が信頼できる Skill に安全。リスク: 副作用の重複。 |
| `skip` | 空/デフォルト完了を合成して続行。リスク: 下流でのデータ欠損。 |
| `discard_skill` | Skill 実行全体を中止し、チェックポイントを破棄して発生元チェーンに失敗を通知。 |
| `prompt` | レガシー/no-op。設定互換性のために保持。自動レジューム ランタイムでは `retry` として扱われます（インタラクティブプロンプトは表示されません）。 |

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `default` | 文字列 | `retry` | 全 Skill のデフォルトレジューム ポリシー。 |
| `per_skill` | マップ | `{}` | Skill ごとのポリシーオーバーライド。キーは Skill 名、値は上記ポリシーのいずれか。 |

## `self_improvement` ブロック

`skill_improver` の動作設定（FP-0006）。Skill 改善提案をソースに適用する方法を制御します。

```yaml
self_improvement:
  on_propose: ask_user   # ask_user | auto | disabled
  max_versions: 10       # 保持する v<N>.md スナップショットの上限; 0 = 制限なし
```

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `on_propose` | 文字列 | `ask_user` | 改善を適用しようとする際の動作。`ask_user` — InterventionBus 経由でユーザーに確認（安全なデフォルト）。`auto` — プロンプトをスキップして直接適用（CI / 無人実行向け）。`disabled` — `skill_improvement_dry_run` イベントをログに記録し変更を適用しない。 |
| `max_versions` | int | `10` | `.reyn/skill-versions/<name>/` に保持する `v<N>.md` スナップショットの上限。上限を超えると最古バージョンが削除されます（current バージョンは削除されません）。`0` でプルーニングを無効化。 |

## `python` ブロック

Python preprocessor 設定。セーフモードでインポートできるモジュールの組み込み許可リストを拡張します。

```yaml
python:
  allowed_modules:
    - math
    - statistics
    - json
    - re
```

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `allowed_modules` | list[string] | `[]` | セーフモード Python preprocessor ステップが組み込み stdlib 許可リストに加えてインポートできる追加モジュール名。内部で I/O を行うライブラリ（例: `pandas`、`requests`）はセーフモードのサンドボックスを無効化します — 慎重に管理してください。 |

> unsafe Python ステップ（preprocessor フロントマターの `mode: unsafe`）はこのリストで制限されず、ランタイムで `--allow-unsafe-python` も必要です。完全な Permission 文法は [Reference: permissions](permissions.md) を参照してください。

## 関連情報

- `reference/config/permissions.md` — 完全な Permission 文法
- `reference/config/state-dir.md` — `.reyn/` レイアウト
- [コンセプト: シークレット管理](../../concepts/secret-handling.md) — `~/.reyn/secrets.env` と `${VAR}` interpolation
- [Reference: `reyn secret`](../../reference/cli/secret.md) — CLI によるシークレット管理
- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — MCP サーバー管理 CLI
