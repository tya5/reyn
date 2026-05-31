---
type: concept
topic: security
audience: [human, agent]
---

# シークレット管理

reyn は MCP サーバーの認証情報、LLM API キー、Web サーバーの TLS 証明書、将来の統合機能まで、すべてのシークレットを単一のユニバーサルな仕組みで管理します。

メンタルモデルはシンプルです。**シークレットは `~/.reyn/secrets.env` に置く。設定ファイルは `${VAR}` で参照する。すべての reyn コンポーネントは `os.environ` 経由で参照する。**

## シークレットの置き場所

```
~/.reyn/secrets.env        # chmod 600 — すべてのシークレットを置く唯一の場所
```

ファイルは標準的な [dotenv](https://github.com/motdotla/dotenv) 形式です。1 行に `KEY=value` を 1 ペア、`#` コメントが使用できます：

```
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxxxxxxxxx
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxx
LITELLM_PROXY_TOKEN=Bearer sk-my-proxy-token
```

**セキュリティの特性：**

- 最初に書き込む際は `chmod 600` で作成されます。起動時にパーミッションが広すぎる場合は警告を出して自動修正します。
- git にはコミットされません。ファイルは `~/.reyn/` にあり、プロジェクトルートの外です。
- reyn のコマンドが値を表示することはありません。`reyn secret list` はキー名とステータスのみを表示します。
- サブプロセスに継承されます（意図的な設計）。reyn が起動した MCP サーバーや Python プリプロセッサーは同じ環境変数を参照します。トレースダンプ（`REYN_LLM_TRACE_DUMP`）は既知のシークレットパターンを自動的に隠蔽します。

## `${VAR}` interpolation

reyn のすべての YAML ファイルのすべての文字列フィールドで、`${VAR}` 構文を使って環境変数を参照できます。変数は起動時（`secrets.env` をロードした後）に `os.environ` から解決されるため、`~/.reyn/secrets.env` の値はどこからでも参照できます：

```yaml
# reyn.yaml — 以下の ${VAR} 参照はすべて secrets.env またはシェル環境変数から解決されます
models:
  default-sonnet:
    model: claude-sonnet-4-5
    api_key: ${ANTHROPIC_API_KEY}          # LLM API キー
    extra_body:
      headers:
        Authorization: ${LITELLM_PROXY_TOKEN}

litellm:
  api_base: ${LITELLM_API_BASE}

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
- シェルの環境変数は `secrets.env` の値より優先されます（1 回の実行のみシェルからオーバーライドできます）。

## ロードタイミング

reyn は `~/.reyn/secrets.env` をプロセス起動時に一度だけ、どのコンポーネントが初期化される前にロードします。これにより：

- すべての reyn コンポーネント（`LiteLLMClient`、MCP の `expand_env()`、Web サーバーなど）は `secrets.env` を知ることなく通常の `os.environ.get()` でシークレット値を参照できます。
- YAML の `${VAR}` interpolation はすでにロードされた環境変数に対して解決されます。
- シークレットの変更を反映するには reyn プロセスを再起動してください。（ゼロ再起動ローテーションのための `reyn secret reload` コマンドはフェーズ 2 の予定です。）

**ロード失敗ポリシー：** `secrets.env` が存在しない場合、起動は黙って続行します。ファイルが存在しても解析エラーがある場合は、問題のある行ごとに警告を出してスキップします。起動は中断しません。

## `reyn secret` CLI

`reyn secret` サブコマンドが `~/.reyn/secrets.env` を管理する主な手段です。完全な構文については [Reference: `reyn secret`](../../reference/cli/secret.md) を参照してください。典型的なフロー：

### 初回セットアップ

```bash
# LLM API キーを追加
reyn secret set ANTHROPIC_API_KEY

# Value for ANTHROPIC_API_KEY: ****   ← 非表示入力
# Secret 'ANTHROPIC_API_KEY' saved to ~/.reyn/secrets.env

# 存在確認（値は表示されない）
reyn secret list
```

`list` の出力：

```
KEY                           STATUS
─────────────────────────────────────
ANTHROPIC_API_KEY             set
GITHUB_PERSONAL_ACCESS_TOKEN  set
OPENAI_API_KEY                stored (not yet in env)
```

### インラインの値（スクリプト / CI）

```bash
reyn secret set ANTHROPIC_API_KEY=sk-ant-xxxxx
```

### ローテーション

```bash
reyn secret rotate ANTHROPIC_API_KEY
# Value for ANTHROPIC_API_KEY: ****   ← 新しい値、非表示入力
# Secret 'ANTHROPIC_API_KEY' rotated in ~/.reyn/secrets.env
```

`rotate` は意味的に `set` と同一ですが、監査ログに `secret_rotated` を記録し、古い値が置き換えられたことを監査消費者に通知します。

### 削除

```bash
reyn secret clear GITHUB_PERSONAL_ACCESS_TOKEN
```

### MCP-aware ショートカット

MCP サーバーをインストールする際、reyn は必要な認証情報を自動的にプロンプトし、同じ仕組みで保存します：

```bash
reyn mcp install github
# github には GITHUB_PERSONAL_ACCESS_TOKEN が必要です。
# 取得方法: https://github.com/settings/personal-access-tokens/new
# GITHUB_PERSONAL_ACCESS_TOKEN: ****
# ✓ github を追加しました。
```

インストール済みのサーバーに認証情報を追加・ローテーションするには：

```bash
reyn mcp set-secret github GITHUB_PERSONAL_ACCESS_TOKEN
```

これは `reyn secret set` の薄いラッパーで、サーバーの env 宣言を読んで適切なキー名を提案します。

## 監査ログ

すべての変更系 `reyn secret` コマンドは P6 監査イベントを発行します。値はイベントペイロードで完全にマスクされます：

| イベント | トリガー | ペイロード |
|--------|---------|-----------|
| `secret_set` | `reyn secret set` | `key`、`value_masked: "***"` |
| `secret_cleared` | `reyn secret clear` | `key` |
| `secret_rotated` | `reyn secret rotate` | `key`、`value_masked: "***"` |

フィルタリング：

```bash
grep '"secret_' .reyn/events.jsonl
```

## セキュリティモデル

**`~/.reyn/secrets.env` が保護するもの：**

- 認証情報の VCS への誤コミット（ファイルはプロジェクトルートの外にある）。
- グループや全体への読み取り権限（reyn が警告とともに 600 に自動修正）。
- CLI 出力への誤表示（すべてのコマンドが値をマスク）。

**保護しないもの：**

- 同一マシン上の侵害されたユーザーアカウント（そのユーザーはファイルを読める）。
- 同じユーザーとして実行されているプロセス — 環境を継承し `os.environ` を読める。
- reyn はボルトでも HSM でもありません。エンタープライズのシークレット管理（HashiCorp Vault、AWS Secrets Manager、macOS Keychain）には、それらのシステムを使って reyn 起動前にシェル環境変数を設定してください。`${VAR}` interpolation が透過的に値を取得します。

## 設定ファイルのスコープ tier との関係

`~/.reyn/secrets.env` はユーザーグローバルです。現在のリリースではプロジェクトスコープの `secrets.env` はありません。推奨パターン：

- プロジェクト固有のシークレット**キー**は `${VAR}` 参照としてプロジェクトの `reyn.yaml`（git にコミット、実際の値を含まない）に宣言します。
- シークレットの**値**は `~/.reyn/secrets.env`（ユーザーグローバル、git に入らない）に置きます。
- マシンごとの非シークレット設定は `reyn.local.yaml`、シークレット値は `~/.reyn/secrets.env` を使います。

## OAuth トークンライフサイクル (FP-0016 B)

静的 dotenv パス（`~/.reyn/secrets.env`、chmod 600）は手動でローテーションする **API キー**向けの設計です。自動更新が必要なトークンには別の仕組みが必要です。

reyn は `~/.reyn/oauth_tokens.json`（chmod 600）に格納される `OAuthToken` 値型を提供します。各エントリにはアクセストークン、リフレッシュトークン、有効期限タイムスタンプ、トークンエンドポイント URL が含まれます。

**ランタイム API** — スキルは OAuth トークンを次の方法でアクセスします：

```python
from reyn.secrets import get_valid_token
token = await get_valid_token("github_oauth")
```

`get_valid_token(key)` の動作：

- トークンが **60 秒以内**に期限切れになる場合、返す前に RFC 6749 §6（リフレッシュトークングラント）でリフレッシュします。
- リフレッシュ成功時：新しいトークンを `~/.reyn/oauth_tokens.json` に永続化し、`token_refreshed` P6 イベントを発行し、新しい `access_token` を返します。
- リフレッシュ失敗時：`token_refresh_failed` を発行し、`OAuthRefreshError` を raise します。呼び出し元がキャッチしてオペレーターに通知します。
- 同じキーへの並行リフレッシュ試行はキーごとの `asyncio.Lock` でシリアライズされます（二重リフレッシュ競合を防止）。

**発行される P6 イベント一覧：**

| イベント | トリガー |
|---------|---------|
| `token_refreshed` | リフレッシュ成功。ペイロードに `key`、マスクされたトークンヒントを含む |
| `token_refresh_failed` | リフレッシュリクエスト失敗。ペイロードに `key`、`error` を含む |

## スキルごとの認証情報スコーピング (FP-0016 D)

**脅威モデル：** 信頼されないドキュメントを処理するサブスキルは、親スキルの全シークレットストアを外部に持ち出すようにプロンプトインジェクションされる可能性があります（Confused Deputy 攻撃）。スコーピングはこれを防ぎます。

### 宣言

各スキルは `skill.md` フロントマターで `required_credentials` を宣言します：

```yaml
# skill.md フロントマター
---
name: pr-reviewer
required_credentials:
  - github_token
---
```

指定できる値：

| 値 | 意味 |
|----|------|
| `[]` | 認証情報不要（stdlib スキルのデフォルト） |
| `["github_token", "openai_key"]` | 明示的な許可リスト |
| `["*"]` | 完全委任 — フィールド省略時の後方互換デフォルト |

### 強制適用

`run_skill` の境界でOS が `ScopedSecretStore(allowed_keys=...)` を構築し、親のスコープと**交差**させます（親キャップセマンティクス — サブスキルが親より広いアクセスを持つことはできません）。

許可セット外の読み取りは `CredentialScopeError`（`PermissionError` のサブクラス）を raise します。

すべてのスコープ決定は監査用の `sub_skill_credential_scope` P6 イベントを発行します：

```json
{"skill": "<name>", "allowed_keys": ["github_token"]}
```

**クロスリファレンス：**

- [コンセプト: パーミッションモデル](../runtime/permission-model.md) "スキルごとの認証情報スコーピング" — ケイパビリティ継承ルールを含む詳細解説。
- [Reference: `skill.md` DSL](../../reference/dsl/skill-md.md) — `required_credentials` フィールドの完全なリファレンス。

## デバイス認可グラント (FP-0016 C)

ブラウザリダイレクトが必要な OAuth フロー（ヘッドレスエージェント環境では使用不可）に対して、reyn は RFC 8628 デバイス認可グラントを次の CLI で実装しています：

```bash
reyn auth login <provider>
```

コマンドはユーザーコードと確認 URL を表示し、トークンエンドポイントをポーリングし、取得したトークンを `~/.reyn/oauth_tokens.json` に格納して `get_valid_token` から使えるようにします。ブラウザ自動化やコールバックサーバーは不要 — オペレーターが自分のデバイスで URL を開いて承認します。

- 完全な CLI 使用方法：[Reference: `reyn auth`](../../reference/cli/auth.md)
- プロバイダー設定（`oauth.providers`）：[Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md)

## エージェント ID (FP-0016 E)

すべての P6 イベントおよびすべての外部 HTTP 呼び出し（MCP、将来の A2A）はエージェント ID を含みます。ID は `reyn.yaml` の `agent.id` フィールドから取得され、省略時は `reyn/<hostname>` がデフォルトです。

ID が登場する場所：

- イベントペイロードの `agent_id` フィールド。
- 外部 HTTP リクエストの `X-Reyn-Agent-Id` ヘッダー。
- A2A タスクエンベロープの `initiator` フィールド。

クロスエージェントトレーシングとマルチエージェントトポロジについては：[コンセプト: マルチエージェント](../multi-agent/multi-agent.md) "エージェント ID 伝播"。

## 関連項目

- [Reference: `reyn secret`](../../reference/cli/secret.md) — 完全な CLI 構文
- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — `set-secret` / `clear-secret` サブコマンド
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md) — 設定フィールドでの `${VAR}` interpolation；OAuth プロバイダー設定
- [Reference: `reyn auth`](../../reference/cli/auth.md) — デバイス認可グラント CLI
- [Reference: `skill.md` DSL](../../reference/dsl/skill-md.md) — `required_credentials` フィールドリファレンス
- [コンセプト: パーミッションモデル](../runtime/permission-model.md) — `mcp_install` パーミッションゲート；スキルごとの認証情報スコーピング
- [コンセプト: マルチエージェント](../multi-agent/multi-agent.md) — エージェント ID 伝播
- ADR-0030 `docs/deep-dives/decisions/0030-universal-../runtime/secret-handling.md` — 設計の根拠（実装チーム向け、内部）
