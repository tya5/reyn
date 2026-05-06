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
| `limits` | マップ | ランタイム上限: Phase 訪問数、ウォールクロックバジェット、LLM タイムアウト/リトライ。以下参照。 |
| `state_dir` | パス | Reyn がイベント、承認、Memory を書き込む場所。デフォルト `.reyn/`。 |
| `permissions` | マップ | デフォルトの Permission ポリシー。以下参照。 |

## `models` ブロック

`models:` の各エントリはクラス名を LiteLLM モデル文字列 **または** per-class LLM パラメータを宣言する dict にマップします。

### str 形式（後方互換）

```yaml
models:
  light:    openai/gemini-2.5-flash-lite
  standard: openai/gpt-4o
  strong:   anthropic/claude-3-5-sonnet-20241022
```

str 形式を使用している既存の `reyn.yaml` はすべて変更なしで動作します。

### dict 形式（opt-in、PR-MODEL-SPEC）

```yaml
models:
  standard: openai/gemini-2.5-flash-lite   # str 形式も dict エントリと併用可能

  strong:
    model: anthropic/claude-3-7-sonnet      # 必須
    temperature: 0.0
    max_tokens: 16000
    extra_body:
      thinking:
        type: enabled
        budget_tokens: 8000
```

| フィールド | 必須 | 説明 |
|-------|----------|-------------|
| `model` | はい | LiteLLM モデル文字列。 |
| `temperature` | いいえ | litellm に渡すサンプリング温度。 |
| `max_tokens` | いいえ | litellm に渡す最大出力トークン数。 |
| `top_p` | いいえ | litellm に渡す top-p サンプリング。 |
| `extra_body` | いいえ | プロバイダー固有のペイロード（例：推論モデルの `thinking`）。 |
| *（その他のフィールド）* | いいえ | litellm にそのまま渡されます（パススルーポリシー）。 |

**フィールドポリシー**: `model` のみ必須です。他のフィールドはすべてバリデーションなしで `litellm.acompletion` に直接渡されます（未知のフィールドも silent に転送されます — future-proof）。タイポは reyn エラーではなく silent な litellm 失敗を引き起こします。

**Skill / Phase 側オーバーライド**: サポートしていません。Operator config（`reyn.yaml`）が LLM パラメータの唯一の source of truth です。Skill 作者はクラス名のみを指定します（例：`model_class: strong`）。

**マージ順**: Reyn が管理する設定（`timeout`、`num_retries`、プロキシルーティング）は operator 宣言の kwargs より常に優先されます。

## `limits` ブロック

ランタイム上限の中央設定。各値は対応する CLI フラグで呼び出しごとにオーバーライドできます。

```yaml
limits:
  llm:
    timeout: 60        # LLM HTTP 呼び出しごとの秒数 (--llm-timeout)
    max_retries: 3     # 呼び出しごとの一時的エラーのリトライ数 (--llm-max-retries)
  phase:
    max_visits: 25         # ランごとの Phase あたりの上限; 0 = 無制限 (--max-phase-visits)
    max_wall_seconds: 0    # Phase ごとのウォールクロックバジェット; 0 = 無制限 (--phase-budget)
```

| パス | 型 | デフォルト | 説明 |
|------|------|---------|-------------|
| `limits.llm.timeout` | float（秒） | `60` | LiteLLM に渡される呼び出しごとの HTTP タイムアウト。 |
| `limits.llm.max_retries` | int | `3` | LLM 呼び出しごとの一時的エラーのリトライ数（LiteLLM 指数バックオフ）。 |
| `limits.phase.max_visits` | int | `25` | ランごとの任意の単一 Phase への再訪問上限。`0` = 無制限。 |
| `limits.phase.max_wall_seconds` | float（秒） | `0` | Phase ごとのウォールクロックバジェット。リトライ/ターンの境界でのソフトチェック。呼び出し途中はキャンセルしない。`0` = 無制限。 |

レガシーのトップレベル `max_phase_visits` キーは引き続き（非推奨警告付きで）受け付けられ、`limits.phase.max_visits` に移行されます。

## `permissions` ブロック

プロジェクト全体のケイパビリティデフォルト。`skill.md` の Skill ごとの Permission がこれらをオーバーライドします。

```yaml
permissions:
  shell: deny           # deny | ask | allow
  file:
    read:  [".reyn/", "src/stdlib/"]
    write: [".reyn/state/", "reyn/local/"]
  python:
    pure:    allow      # pure モードの python ステップのデフォルト
    trusted: deny       # trusted モードには --allow-untrusted-python も必要
    allowed_modules:
      - math
      - statistics
      - json
      - re
```

完全な Permission 文法は `reference/config/permissions.md` に記載されています。

## API キー

API キーは環境変数から来なければなりません。`OPENAI_API_KEY`、`ANTHROPIC_API_KEY`、`GEMINI_API_KEY` などです。`reyn.yaml` や `reyn.local.yaml` には絶対に書かないでください。

## プロキシ / `api_base`

モデルをローカルの LiteLLM プロキシ経由でルーティングする場合は、URL を `reyn.yaml` ではなく `reyn.local.yaml`（gitignored）に書きます:

```yaml
# reyn.local.yaml
api_base: http://localhost:4000
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

## 関連情報

- `reference/config/permissions.md` — 完全な Permission 文法
- `reference/config/state-dir.md` — `.reyn/` レイアウト
