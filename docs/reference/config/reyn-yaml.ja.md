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
| `plan` | マップ | プランモードのステップバジェットとリトライ設定。以下参照。 |
| `web` | マップ | `web_fetch` と MCP レジストリ呼び出しの SSL 設定。以下参照。 |
| `eval` | マップ | `reyn eval` のトレース exporter バックエンド。以下参照。 |
| `sandbox` | マップ | `sandboxed_exec` のバックエンド選択と非対応プラットフォームポリシー。以下参照。 |
| `state_dir` | パス | Reyn がイベント、承認、Memory を書き込む場所。デフォルト `.reyn/`。 |
| `permissions` | マップ | デフォルトの Permission ポリシー。以下参照。 |

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
    unsafe:  deny       # unsafe モードには --allow-untrusted-python も必要
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

  # チェーン + Skill ごとの上限（メモリ内）
  per_chain_skill_calls:
    hard_limit: 5        # 同じ Skill がチェーンで 5 回以上起動されると拒否
  per_chain_skill_tokens:
    hard_limit: 100000   # 1 つの Skill がチェーンで 100k トークン以上蓄積すると拒否

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
| `per_chain_skill_calls` | チェーン+Skill ごと | メモリ内 | チェーン解決または `/budget reset` |
| `per_chain_skill_tokens` | チェーン+Skill ごと | メモリ内 | チェーン解決または `/budget reset` |
| `rate_limit_per_minute` | モデルごと | メモリ内（60 秒ウィンドウ） | 自動（スライディングウィンドウ） |
| `daily_tokens` | プロセスグローバル | 台帳ファイル | 午前 0 時（現地時間） |
| `daily_cost_usd` | プロセスグローバル | 台帳ファイル | 午前 0 時（現地時間） |
| `monthly_tokens` | プロセスグローバル | 台帳ファイル | 月初（現地時間） |
| `monthly_cost_usd` | プロセスグローバル | 台帳ファイル | 月初（現地時間） |

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

[コンセプト: MCP](../../concepts/mcp.md) でプロトコル概要、[How-to: MCP サーバーを使う](../../guide/for-skill-authors/use-an-mcp-server.md) でエンドツーエンドのクイックスタートを参照してください。

## 関連情報

- `reference/config/permissions.md` — 完全な Permission 文法
- `reference/config/state-dir.md` — `.reyn/` レイアウト
- [コンセプト: シークレット管理](../../concepts/secret-handling.md) — `~/.reyn/secrets.env` と `${VAR}` interpolation
- [Reference: `reyn secret`](../../reference/cli/secret.md) — CLI によるシークレット管理
- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — MCP サーバー管理 CLI
