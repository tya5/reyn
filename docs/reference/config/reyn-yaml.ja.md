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
  light:    gemini-flash-lite
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
| `web` | マップ | `web_fetch` と MCP レジストリ呼び出しの SSL 設定。以下参照。 |
| `sandbox` | マップ | `sandboxed_exec` のバックエンド選択・非対応プラットフォームポリシー・agent-level サンドボックスポリシー。以下参照。 |
| `action_retrieval` | マップ | ユニバーサルカタログの可視化 + 検索設定。以下参照。 |
| `embedding` | マップ | RAG 埋め込みモデルクラスとバッチ設定。以下参照。 |
| `chat` | マップ | チャットセッションの Head/Body/Tail 圧縮設定。以下参照。 |
| `voice` | マップ | ⚠️ 現在利用不可(consumerなし)。以下参照。 |
| `events` | マップ | チャットセッションイベントファイルの監査ログローテーションポリシー。以下参照。 |
| `observability` | マップ | P6 監査イベントの OpenTelemetry (OTLP) エクスポート（オプトイン）。デフォルトは無効。以下参照。 |
| `mcp` | マップ | MCP サーバー定義と `search_threshold`。以下参照。 |
| `python` | マップ | Python preprocessor の追加許可モジュール。以下参照。 |
| `agent` | マップ | P6 イベント監査証跡と送信 HTTP ヘッダー用のエージェント識別子。以下参照。 |
| `auth` | マップ | `reyn auth login` 用の OAuth プロバイダー設定。以下参照。 |
| `cron` | マップ | スケジュール付きスキル実行。以下参照。 |
| `external_transports` | マップ | チャット向け受信トランスポート → MCP ツールルーティング（Slack / LINE / Discord など）。以下参照。 |
| `multimodal` | マップ | バイナリメディア（画像・音声）のサイズ上限・超過時の挙動・アーティファクト保存先。以下参照。 |
| `permissions` | マップ | デフォルトの Permission ポリシー。以下参照。 |
| `prompt_cache_enabled` | bool | システムプロンプトに Anthropic プロンプトキャッシュマーカーを付与。デフォルト `true`。 |
| `project_context_path` | 文字列 | すべての Phase システムプロンプトに注入する Markdown ファイル。未設定（デフォルト）: cross-tool 標準を auto-resolve — `AGENTS.md` があればそれ、なければ `REYN.md`（legacy fallback）。明示パスで 1 ファイルに固定、`""` で無効化。下記の注記参照。 |
| `api_base` | 文字列 | LiteLLM プロキシベース URL。通常は `reyn.local.yaml`（gitignored）に設定。 |

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

## `models` ブロック

`models:` の各エントリはクラス名を LiteLLM モデル文字列 **または** per-class LLM パラメータを宣言する dict にマップします。

### モデルクラス と モデル名 — 解決ルール

config には2種類の位置があり、逆のルールに従います。同じルールが補完側 `models:` ブロック **と** `embedding.classes:` ブロックの両方に適用されます。

- **クラス位置**（クラスへの *参照*）：`model`、per-agent / per-phase / per-op のモデル上書き、`embedding_class`。これらは **closed-world** — 値は `models:` / `embedding.classes:` に存在するクラス（または組み込み tier: `light` / `standard` / `strong`）を指さなければなりません。既知クラスでない値は、リテラルモデルとして黙って素通しされません：
  - オペレータ config（reyn.yaml の `model:`）は後方互換のリテラル素通しを維持（`openai/gpt-4o` を直接書ける）；
  - **skill/op 由来**のモデル（`op.model`）が既知クラスでない場合は **reject** され、runtime モデルにフォールバック（警告1件）します。これにより skill・LLM 由来の文字列が proxy config を迂回できません — モデル選択の単一の真実源は proxy config です。
- **名前位置**（モデルの *定義*）：`models:` / `embedding.classes:` エントリ内の `model:` 値。名前は `provider/model`（例：`openai/gpt-4o`、`sentence-transformers/all-MiniLM-L6-v2`）であるべきです。`/` のない bare 名は許容されます（一部の LiteLLM 文字列は bare）が、ロード時に **警告** します — 解決が誤ルートする場合は prefix を追加してください。

一言で：**`_class` / tier 位置はクラス名（closed-world）、`model` 位置は `provider/model`（検証付き）。どちらも受け付ける位置はない。**

### str 形式 — リテラル（後方互換）

str 値に **`/` が含まれる** 場合、LiteLLM モデル文字列として直接使用されます：

```yaml
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
models:
  standard: claude-sonnet-thinking     # 等価: standard: {extends: claude-sonnet-thinking}
```

不明な省略形（ユーザーエントリにも built-in にも存在しない名前）は起動エラーになります。

### dict 形式 — plain kwargs

```yaml
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
| *（その他のフィールド）* | いいえ | litellm にそのまま渡されます（パススルーポリシー）。 |

> **コスト制限**: `max_tokens` ではなく `max_completion_tokens` を使用してください。`max_tokens` は多くのプロバイダーが無視するレガシーのソフトヒントです。`max_completion_tokens` は API レベルで強制されます（OpenAI o1+ および Anthropic モデル）。

**フィールドポリシー**: `model` のみ必須です。ほとんどのフィールドはバリデーションなしで `litellm.acompletion` に直接渡されます（未知のフィールドも silent に転送されます — future-proof）。タイポは reyn エラーではなく silent な litellm 失敗を引き起こします。唯一の例外は `reasoning_effort` で、ロード時にバリデーションされます（下記）。

### `reasoning_effort`（モデルごとの推論バジェット）

モデルが回答前にどれだけ「思考」するかを設定します。分かりやすさのためモデル定義ごとに宣言します:

```yaml
models:
  light:
    model: gemini-flash-lite
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
| `gemini-flash-lite` | `gemini/gemini-2.5-flash-lite` | |
| `gemini-3.1-flash-preview` | `gemini/gemini-3.1-flash-preview` | |
| `gemini-2.0-flash` | `gemini/gemini-2.0-flash` | `thinking_budget=0` で thinking 無効化 |

ユーザー宣言エントリは同名の built-in を**上書き**します。built-in カタログは便利な出発点であり、`reyn.yaml` が常に source of truth です。

詳細は [Reference: built-in models](../builtin-models.md) を参照してください。

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
    phase_seconds: 0             # Phase ごとのウォールクロックバジェット; 0 = 無制限 (--phase-budget)
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
| `safety.timeout.phase_seconds` | float（秒） | `0` | `--phase-budget` | Phase ごとのウォールクロックバジェット。リトライ/ターンの境界でのソフトチェック。呼び出し途中はキャンセルしない。`0` = 無制限。 |
| `safety.timeout.chain_seconds` | float（秒） | `60` | — | マルチエージェントチェーンがデリゲート返答を待機する時間。上限超過後にランタイムが上流エラーを生成。`0` = 無効。 |

### `safety.on_limit` フィールド

| パス | 型 | デフォルト | 説明 |
|------|------|---------|-------------|
| `safety.on_limit.mode` | 文字列 | `interactive` | ループ/タイムアウト上限発動時の動作。`interactive`（デフォルト） — `ask_user` でユーザーに延長許可を確認。ヘッドレス（bus=None / 非 TTY）は自動的に abort へ短絡。`unattended` — 即時中止（CI / cron / スクリプト実行向けのオプトイン）。`auto_extend` — `auto_extend_times` 回自動延長後に中止。 |
| `safety.on_limit.auto_extend_times` | int | `1` | abort フォールスルーまでの自動延長回数。`mode: auto_extend` 時のみ使用。 |
| `safety.on_limit.ask_timeout_seconds` | float（秒） | `0` | `interactive` モードでユーザー返答を待機する時間。`0`（デフォルト） = 無制限待機、正の値 = ウィンドウ経過で partial data として abort。 |

## `web` ブロック

`web_fetch` と MCP パッケージレジストリの SSL 設定。

```yaml
web:
  fetch:
    verify_ssl: true     # true | false | 省略（デフォルト: 環境変数チェーン）
    ca_bundle: /path/to/ca-bundle.pem   # 省略可; カスタム CA バンドル
    max_download_bytes: 10485760        # ワイヤバイト上限（デフォルト 10MB）
    allow_private_ips: false            # SSRF: プライベート IP への opt-in（デフォルト deny）
  ws_max_size: 16777216  # WebSocket インバウンドフレーム上限（デフォルト 16MB）
```

優先度チェーン（高い順）:

| 優先度 | 条件 | 有効な SSL 設定 |
|--------|------|----------------|
| 1 | `web.fetch.ca_bundle` 設定あり | カスタム CA バンドルファイル（`verify=<path>`） |
| 2 | `web.fetch.verify_ssl: false` | SSL 検証を無効化（`verify=False`）— **管理された環境のみ** |
| 3 | `web.fetch.verify_ssl: true` | SSL 検証を強制（`verify=True`） |
| 4 | 両方省略 | フォールスルー: `SSL_VERIFY` 環境変数 → `litellm.ssl_verify` → `SSL_CERT_FILE` → `True` |

`verify_ssl` と `ca_bundle` は MCP レジストリの HTTP 呼び出し（パッケージインストール）にも適用されます。

`web.fetch.max_download_bytes`（int, デフォルト `10485760` = 10MB）は `web_fetch` がワイヤから読み取るレスポンスの最大バイト数。`Content-Length` がこの値を超えるレスポンスは本文ダウンロード前に拒否され、chunked / 長さ不明の本文はストリームが上限を超えた時点で中断されます（ステータス `too_large`）。悪意ある / 暴走 URL による無制限な本文のメモリ枯渇を防ぎます。`<= 0` / 非整数はデフォルトにフォールバック。

`web.fetch.allow_private_ips`（bool, デフォルト `false`）は SSRF 対策。`true` のとき `web_fetch` / `safe.http` がプライベート RFC1918/ULA アドレスへ到達できます（エンタープライズの内部 fetch 向け opt-in）。link-local / クラウドメタデータ（`169.254.169.254`）/ ループバックはこのフラグに関わらず**常に**拒否されます。HTTP リダイレクトは hop ごとに再検証（allowlist + IP-deny）されるため、allowlist 済みホストが内部ターゲットへリダイレクトすることはできません。`REYN_FETCH_ALLOW_PRIVATE_IPS` 環境変数にもエクスポートされ、safe.http サブプロセス + レジストリクライアントが同じ opt-in を参照します。

`web.ws_max_size`（int, デフォルト `16777216` = 16MB）は `reyn web` ゲートウェイが受け付ける単一 WebSocket インバウンドフレームの最大バイト数。サーバーライブラリの暗黙デフォルトに依存せず上限を明示的に固定するため、ライブラリアップグレード後も bound が維持されます。operator は tighten / loosen 可能。`<= 0` / 非整数はデフォルトにフォールバック。

## `sandbox` ブロック

`sandboxed_exec` op + OS の in-process file/http ゲートのバックエンド選択・非対応プラットフォームポリシー・agent-level サンドボックスポリシー。

```yaml
sandbox:
  backend: auto          # auto | seatbelt | landlock | noop
  on_unsupported: warn   # warn | error | ignore
  policy:                # オプション — agent-level（オペレータ）サンドボックスポリシー
    network: true
    read_paths: ["/"]
    write_paths: ["{{workspace}}", "/tmp"]
    allow_subprocess: true
    env_passthrough: ["PATH", "HOME"]
    timeout_seconds: 600
```

> ℹ️ **`read_deny_paths` のエントリは、重なる `write_paths` の許可より常に優先されます。**
> Seatbelt バックエンドでは `read_deny_paths` の deny ルールが `write_paths` の allow ルールの
> **後に** emit され、SBPL は last-match-wins のため、credential パス（`~/.ssh`・`~/.aws`・
> `~/.gnupg` 等）を包含する広い `write_paths`（`~`・`$HOME`・`/`）を書いても、そのパスは開き
> ません — deny は**読み取り・書き込みの両方**に効き続け、OS は `sandbox_policy_narrowed`
> audit-event を出して縮小を可視化します（#2978）。設計則: デフォルトの deny-list は、広い
> write 許可が貫けない床（floor）です。denied プレフィックス配下に本当に書き込む必要がある場合は、
> `read_deny_paths` から該当エントリを明示的に外してください（`read_deny_paths` はオペレーター
> 所有で、縮小できます）。それでも `write_paths` は最小限のディレクトリに絞ってください。

| キー | 型 | デフォルト | 説明 |
|-----|------|---------|-------------|
| `backend` | 文字列 | `auto` | 強制バックエンド。`auto` は OS が選択: macOS < 26 → `seatbelt`（sandbox-exec SBPL）、Linux ≥ 5.13 かつ `sandbox-linux` extra インストール済み → `landlock`（+ オプションの seccomp-BPF）、その他 → `noop`（監査のみ、強制なし）。明示的な値で特定バックエンドを強制できます。 |
| `on_unsupported` | 文字列 | `warn` | 使用可能な OS サンドボックスバックエンドが無い場合のポリシー — 要求バックエンドがこのプラットフォームで利用不可の場合に加え、選択されたバックエンドが**封じ込め self-test に失敗した場合**（= 存在するが deny を発火しない。そのバックエンドは「存在しない」場合とまったく同じに扱われる）も含む。`warn` は WARNING をログに記録して `noop` にフォールバック。`error` は `RuntimeError` を発生（強制が必須な本番環境のフェイルファスト。存在するが不活性なバックエンドに対しても効く）。`ignore` はサイレントにフォールバック。 |
| `policy` | マップ | _なし_ | **agent-level（オペレータ）サンドボックスポリシー**。設定すると、サンドボックス op に適用される決定的ポリシーになり、かつ OS の in-process file/http ゲートの permission 積（`∩`）の `SandboxLayer` に畳み込まれます — op 宣言のフィールドに **優先（WINS）** するため、スキルや LLM が緩めることはできません。省略時（デフォルト）は **agent-level の制限なし**: `SandboxLayer` は恒等（`⊤`）のままで op レベルのフィールドが従来通り支配します。サンドボックス認可はオペレータ/run の関心事です。サブキーは以下参照。 |

### `sandbox.policy` サブキー

`sandbox.policy` が存在する場合、`SandboxPolicy` フィールドを反映します。未知のキーは config ロード時に拒否されます。

| キー | 型 | デフォルト | 説明 |
|-----|------|---------|-------------|
| `network` | bool | `DEFAULT_SANDBOX_NETWORK`（現在 `true`） | サンドボックスプロセスからの外向きネットワークを許可。主要な外部流出ゲート。`sandbox.policy` 明示ブロックでこのキーを省略した場合、single-source の床 `DEFAULT_SANDBOX_NETWORK`（現在 `true`）を継承する — `SandboxPolicy` の dataclass デフォルトの `false` **ではない**。部分的な policy はこの床にマージされる（#2964）ため、`network` を省略するとネットワークは ON のまま。隔離するには `network: false` を明示すること。 |
| `write_paths` | list[文字列] | `[]` | プロセスが書き込み可能なパス（厳密なガード）。書き込みは読み取りを含む。`write_paths` の許可と重なる `read_deny_paths` エントリは常に優先される（deny-always-wins、#2978）— ∴ 広い `write_paths` でも denied な credential パスは開かない。`~` は展開される。 |
| `read_deny_paths` | list[文字列] | `~/.ssh`・`~/.aws`・`~/.gnupg`・`~/.config/gcloud`・`~/.kube`・`~/.docker/config.json`・`~/.netrc` | 広読み込みサーフェスから拒否する機密パス（多層防御）— `sandbox.policy` 明示ブロックでこのキーを省略した場合、空リストではなくこの7つの OS レベル credential パス（`SandboxPolicy.read_deny_paths` の dataclass デフォルト）がデフォルトになります。deny-after-allow をサポートするバックエンド（Seatbelt）のみ適用。許可リストのみのバックエンド（Landlock、read-deny プリミティブが無い）では非対応。`write_paths` のエントリがこれらと重なる・包含する場合でも deny を無効化しない — Seatbelt 上では deny が常に勝ち（#2978）、`sandbox_policy_narrowed` audit-event が縮小を記録します。 |
| `read_paths` | list[文字列] | `[]` | **レガシー。** かつての厳密な読み込み許可リスト。現在のスコーピングモデルでは読み込みはデフォルトで広許可のため、このフィールドは意図した読み込み対象のドキュメントとしてのみ機能します。 |
| `allow_subprocess` | bool | `false` | 子プロセスの spawn を許可するか。適用（enforced）— off の時 `process-fork` を deny。 |
| `env_passthrough` | list[文字列] | `[]` | サンドボックスプロセスへ通過させる環境変数名。`PATH` は常に通過します。 |
| `timeout_seconds` | int | `60` | バックエンドが強制する実時間上限。 |

[リファレンス: control-ir — `sandboxed_exec`](../runtime/control-ir.md#sandboxed_exec) で op スキーマとバックエンド選択の詳細を参照してください。

## `action_retrieval` ブロック

ユニバーサルカタログの可視化 + 検索設定。 scheme *選択* は `tool_use` ブロック(EN 版 `reyn-yaml.md#tool_use-block` を参照。ja 版はまだこのブロックの翻訳が無い)に generalize されています — `tool_use.chat` はデフォルトで `enumerate-all`(この wrapper path ではない)です。 `tool_use.chat: universal-category` を設定するとこのフラグが設定する wrapper scheme を選択できます。 chat レイヤーの scheme が `universal-category` に解決される時、 このフラグがその presentation を制御します。 **ユニバーサル wrapper** (`list_actions` / `describe_action` / `invoke_action`) による、 全 skill / agent / MCP / file / memory / RAG カテゴリで統一の browse / describe / invoke を提供します。`universal_wrappers_enabled` は legacy フラグパスの直接呼び出し元に対してデフォルト ON — その呼び出し元について既存の flat `tools=` shape を保持したい operator は `universal_wrappers_enabled: false` でオプトアウト可能。

```yaml
action_retrieval:
  universal_wrappers_enabled: true    # デフォルト; false でオプトアウト
  # embedding_class: local-mini       # デフォルトは null (無効); opt-in するにはコメント解除
  hot_list_n: 0                       # 0 = 無効（デフォルト）; opt-in は例えば 10
  mode: default                       # default | minimal | performance
```

### `action_retrieval` フィールド

| フィールド | 型 | デフォルト | 説明 |
|-----|------|---------|-------------|
| `universal_wrappers_enabled` | bool | `true` | `tool_use` scheme が `universal-category` に解決される layer について、`true`(デフォルト)の時、その layer の `tools=` は 4 universal wrappers (`list_actions` / `search_actions` / `describe_action` / `invoke_action`) + hot list direct aliases のみ。 legacy per-kind tool (`invoke_skill` / `call_mcp_tool` 等) はその layer で LLM に surface されず、 wrapper の backing handler として残存。 `search_actions` は `embedding_class` で別途ゲート。 `false` 設定でその layer の wrapper surface 自体を無効化 (= legacy のみが addressing path)。 scheme が `enumerate-all`(`chat` layer 自身のデフォルト)である layer には影響しない — その scheme はこのフラグを一切参照しない。 |
| `embedding_class` | string \| null | `null` | action-retrieval の semantic 検索に使用する [`embedding.classes`](../../concepts/data-retrieval/rag.md) のエントリ名。 **デフォルト `null` (無効) — semantic `search_actions` は opt-in。** `null` または空の場合、 wrapper が有効でも `search_actions` は `tools=` から除外され、 embedding index の build も一切試行されない (= silent、失敗も警告も発生しない)。 opt-in するには明示的に `local-mini` (= `sentence-transformers/all-MiniLM-L6-v2`; ローカル、`reyn[local-embed]` extras と初回の Hugging Face モデルダウンロードが必要) または `standard` (= OpenAI backed、ローカルダウンロード不要、`OPENAI_API_KEY` が必要) を設定する。 設定すると cold-start session で eager embedding build を発動し初回ターンの hallucination を回避。 **Graceful degrade**: 選んだクラスが `sentence-transformers/` モデルを指すのに `local-embed` extras 未インストールの場合、 reyn は黙って `null` 扱いとし `list_actions` がインストールコマンドを LLM に surface する。 |
| `hot_list_n` | int | `0` | top-N `freq+recency` direct alias のホットリスト投影サイズ。 デフォルト `0` (= 無効) — `list_actions` が正規の discovery path。 opt-in は `10` 以上を設定; seed・usage tracker・alias-builder は完全維持。 |
| `mode` | string | `"default"` | 運用モードラベル: `"minimal"` (キャッシュ安定性最大、 ホットリストなし) / `"default"` (バランス) / `"performance"` (大規模ホットリスト)。 自由文字列で、 呼び出し側がセマンティクスを上乗せ。 |

### クイックスタート — semantic `search_actions` を opt-in

`search_actions` はデフォルトで無効 (`embedding_class: null`) — semantic search はプロジェクト全体で opt-in の方針です。有効にするには:

```yaml
# reyn.yaml — ローカルモデル (`pip install 'reyn[local-embed]'` が必要;
# 初回利用時に Hugging Face から ~22 MB ダウンロード)
action_retrieval:
  embedding_class: local-mini
```

```yaml
# reyn.yaml — API backed、ローカルダウンロード不要 (`OPENAI_API_KEY` が必要)
action_retrieval:
  embedding_class: standard
```

詳細な手順（オフライン/エアギャップ環境のガイダンスを含む）は [ガイド: semantic search を有効にする](../../guide/for-users/enable-semantic-search.ja.md) を参照。

### クイックスタート — オプトアウト

```yaml
# reyn.yaml — legacy の tools= shape を保持
action_retrieval:
  universal_wrappers_enabled: false
```

有効時（デフォルト）、 チャット Router の `tools=` 末尾に wrapper が含まれる。 LLM は以下を呼び出し可能:

- `list_actions(category=["mcp"])` → qualified name 形式(例: `mcp__call_tool`)でカテゴリ内の利用可能 action を列挙
- `describe_action(action_name="mcp__call_tool")` → input schema を取得
- `invoke_action(action_name="mcp__call_tool", args={...})` → 既存 handler 経由で実行

リソースカテゴリ (`mcp.server`, `rag_corpus`, `memory_entry`, …) も `invoke_action` をサポート。 不明な action 名は文字列類似度でランクされた `suggestions` を含む構造化エラーを返し、 LLM は 1 turn で復帰可能。

ツールレジストリ / dispatch の背景は Concepts: architecture (architecture doc removed) を参照。

## `agent` ブロック

監査証跡と HTTP ヘッダー伝播のためのランタイムエージェント識別子。

```yaml
agent:
  id: "reyn/acme/code-review-agent"  # デフォルト: reyn/<hostname>
```

### `agent` フィールド

| フィールド | 型 | デフォルト | 説明 |
|-------|------|---------|-------------|
| `agent.id` | 文字列 | `reyn/<hostname>` | この Reyn インスタンスの安定識別子。すべての P6 イベントペイロードに `agent_id` としてスタンプされ、MCP / A2A / 外部 HTTP リクエストの送信時に `X-Reyn-Agent-Id` ヘッダーとして付与される（SOC2 / ISO27001 / METI v1.1 監査パターン）。推奨フォーマット: `reyn/<org>/<role>`（operator 定義）。空文字列を指定した場合はデフォルトにフォールバックし、空の `agent_id` がイベントやヘッダーに漏れるのを防ぐ。 |

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

## `cron:` ブロック

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
- `docs/concepts/data-retrieval/operational-intelligence.md` — イベントログの定期
  indexing agent をスケジュールする

## `permissions` ブロック

プロジェクト全体のケイパビリティデフォルト。`skill.md` の Skill ごとの Permission がこれらをオーバーライドします。

```yaml
permissions:
  shell: deny           # deny | ask | allow
  file:
    read:  [".reyn/", "src/stdlib/"]
    write: [".reyn/state/", "reyn/local/"]
  python:
    safe:    allow      # python ステップは常にサンドボックス化（safe モードのみ）
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

スコープ層との詳細な連動とエンタープライズユースケースは [コンセプト: パーミッションモデル](../../concepts/runtime/permission-model.md#mcp_install-パーミッション) を参照してください。

完全な Permission 文法は `reference/config/permissions.md` に記載されています。

## `${VAR}` interpolation {#var-interpolation}

`reyn.yaml`（または `reyn.local.yaml` / `~/.reyn/config.yaml`）の任意のセクションの任意の文字列フィールドで、`${VAR}` 構文を使って環境変数を参照できます。変数は起動時、`~/.reyn/secrets.env` を環境変数にロードした後に `os.environ` から解決されます（詳細は [コンセプト: シークレット管理](../../concepts/runtime/secret-handling.md) 参照）。

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

各設定について、Reyn は（優先度が低い方から、後の層が前を上書き）マージします:

1. **組み込みデフォルト** — reyn 同梱の値（例: `model: standard`）。
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
| `call_timeout_seconds` | float | すべて（任意） | MCP SDK の `read_timeout_seconds` に渡される per-call リクエストタイムアウト。 未設定の場合は SDK デフォルトが適用される（= Reyn 側で override しない、 transport-specific の SDK timeout が支配）。 特定 server が遅いと分かっている場合、 あるいは速い + fail-fast したい場合に設定する。 `timeout` (= `type: http` の HTTP transport connect timeout) とは独立。 |
| `auth` | string \| map | すべて（任意、`http` のみ） | サーバーごとの OAuth 2.1 設定。文字列 `"oauth"` または `{type: oauth, scopes?, client_id?, client_secret?}`。`http` トランスポート以外(`stdio`/`sse`)で指定するとエラー。詳細は [コンセプト: MCP § OAuth](../../concepts/tools-integrations/mcp.ja.md#oauth) 参照。 |
| `elicitation` | string | すべて（任意） | `prompt`(デフォルト) — サーバー起動の構造化入力要求(`elicitation/create`)がコンセントプロンプトとして表示される。`auto_decline` — そのようなすべての要求をプロンプトせずに decline する。[コンセプト: MCP § Elicitation](../../concepts/tools-integrations/mcp.ja.md#elicitation) 参照。 |
| `elicitation_timeout_seconds` | float | すべて（任意） | elicitation プロンプトに人間が回答するためのウォールクロック期限。デフォルト `120`。期限を過ぎた未回答の要求はキャンセルされます。 |

サーバーは設定ソースをまたいでマージされます: `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml`。マージは `mcp.servers` キーの shallow union です。マシンごとの `reyn.local.yaml` が残りを再宣言せずに単一サーバーを追加・上書きできます。

MCP ランタイムはコアインストールに同梱されます。`fastmcp` はコア依存（MCP クライアントは各セッションで構築されます）なので extra は不要です。（既存の `pip install -e ".[mcp]"` が解決し続けられるよう、空の `[mcp]` extra を後方互換エイリアスとして残しています。）

### `mcp.search_threshold`

すべての接続済みサーバーにわたる MCP ツールの総数がこの閾値に達すると、`build_tools()` が全 MCP ツールスキーマのインライン展開から Anthropic の `tool_search_tool`（遅延ロードモード）に切り替わります。デフォルト `30`。`0` で無効化。

```yaml
mcp:
  search_threshold: 30   # デフォルト; スキーマを常にインライン化するには 0 に設定
  servers:
    ...
```

[コンセプト: MCP](../../concepts/tools-integrations/mcp.md) でプロトコル概要を参照してください。

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
| `description` | string | `""` | モデル向けの `## Skills` メニューに表示される一行サマリー(最初の行のみ、200 文字上限)。 |
| `enabled` | bool | `true` | `false` にするとエントリはレジストリから完全に除外されます。`visibility` より優先します。 |
| `visibility` | enum | `menu` | どの面が skill を名指すか: `menu`(`## Skills` システムプロンプトメニューに載る)\| `on_demand`(メニューには載らないが `skill_list` ツールが返す — 常駐トークンコストなし)\| `hidden`(どのモデル向け面にも現れない)。 |

`enabled: false` は `visibility` を参照する前にエントリを落とすため、2 つのフィールドが表すのは 6 状態ではなく 4 状態です。

**#2971 で削除: `auto_invoke`**(misnomer — skill を自動起動する機構は無く、メニュー描画だけを制御していた。当時メニューは skill を名指す唯一の面だったため、`false` は「広告しない」ではなく到達不能を意味した)。`auto_invoke` が残った config は load 時にエラーとなり置換先を提示します: `auto_invoke: true` → `visibility: menu`、`auto_invoke: false` → `visibility: hidden`。

`skills.entries` は `~/.reyn/config.yaml` ⊕ `reyn.yaml` ⊕ `reyn.local.yaml` ⊕ 動的な `<project>/.reyn/config/skills.yaml`(`skill_management__install_local` / `skill_management__install_source` chat ツールが書き込む)をまたいでマージされ、名前が衝突した場合は後の tier が優先します — `mcp.servers` と同じマージ形です。

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

RAG 埋め込みモデルクラスとバッチ設定。組み込みデフォルトが OpenAI パスをカバーしているため、`OPENAI_API_KEY` を設定した新規インストールでは `reyn.yaml` の変更は不要です。

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

| クラス | モデル | 備考 |
|-------|-------|-------|
| `light` | `openai/text-embedding-3-small` | `OPENAI_API_KEY` が必要。 |
| `standard` | `openai/text-embedding-3-small` | `OPENAI_API_KEY` が必要。 |
| `strong` | `openai/text-embedding-3-large` | `OPENAI_API_KEY` が必要。 |
| `local-mini` | `sentence-transformers/all-MiniLM-L6-v2` | `pip install 'reyn[local-embed]'` が必須。extras が無い場合、初回 `embed()` 呼び出しで raise（`search_actions` の可視化ゲートは hidden へ graceful degrade）。 |
| `local-e5` | `sentence-transformers/intfloat/multilingual-e5-small` | 同じく `local-embed` extras 必須。多言語モデル（非英語コーパスで recall が向上）。 |

キャッシュロケーション・トレードオフは [Concepts: RAG — local embedding backend](../../concepts/data-retrieval/rag.ja.md#local-embedding-backend-fp-0043) を参照。

## `chat` ブロック

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

| フィールド | デフォルト | 説明 |
|-------|---------|-------------|
| `topic_arc` | `200` | トピックアークサマリーセクションのトークン上限。 |
| `decisions` | `400` | 決定事項セクションのトークン上限。 |
| `pending` | `400` | 保留項目セクションのトークン上限。 |
| `session_user_facts` | `200` | 圧縮をまたいで引き継ぐユーザーファクトのトークン上限。 |
| `artifacts_referenced` | `300` | アーティファクト参照一覧のトークン上限。 |

### 廃止キー

`head_size`、`tail_size`、`trigger_total_tokens`、`min_compact_batch` は認識されなくなりました。
`reyn.yaml` に存在する場合、Reyn は起動時に `DeprecationWarning` を発行して無視します。
これらのキーを設定ファイルから削除してください — head/tail のサイズ管理は `component_weights`
によるトークンバジェットに移行し、自動圧縮はウィンドウ相対になりました。

## `events` ブロック

チャットセッションイベントファイルの監査ログローテーションポリシー。Skill 実行イベントはラン 1 つにつき 1 ファイルを使用し、この設定の影響を受けません。

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

> Python ステップは常にサンドボックス化されます。`mode: unsafe` の宣言はロード時に拒否されます — 生の I/O は `run_op` ステップに分離するか、permission でゲートされた `reyn.api.safe.*` サーフェスを使用してください。完全な Permission 文法は [Reference: permissions](permissions.md) を参照してください。

## `multimodal` ブロック

Reyn がバイナリメディア（`web__fetch` / `file__read` / MCP サーバー由来の画像）を扱う方法と、マルチモーダルアーティファクトのディスク上の保存先を制御します。

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
