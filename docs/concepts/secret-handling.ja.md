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

`reyn secret` サブコマンドが `~/.reyn/secrets.env` を管理する主な手段です。完全な構文については [Reference: `reyn secret`](../reference/cli/secret.md) を参照してください。典型的なフロー：

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

## 関連項目

- [Reference: `reyn secret`](../reference/cli/secret.md) — 完全な CLI 構文
- [Reference: `reyn mcp`](../reference/cli/mcp.md) — `set-secret` / `clear-secret` サブコマンド
- [Reference: `reyn.yaml`](../reference/config/reyn-yaml.md) — 設定フィールドでの `${VAR}` interpolation
- [コンセプト: パーミッションモデル](permission-model.md) — `mcp_install` パーミッションゲート
- ADR-0030 `docs/deep-dives/decisions/0030-universal-secret-handling.md` — 設計の根拠（実装チーム向け、内部）
