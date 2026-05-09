---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn secret]
---

# `reyn secret`

`~/.reyn/secrets.env` に保存されたシークレットを管理します。メンタルモデルとセキュリティの特性については [コンセプト: シークレット管理](../../concepts/secret-handling.md) を参照してください。

## 概要

```
reyn secret set <KEY>[=<VALUE>]
reyn secret list
reyn secret clear <KEY>
reyn secret rotate <KEY>[=<VALUE>]
```

## 説明

`reyn secret` は、すべての reyn コンポーネントが使用するユニバーサルシークレットストアである `~/.reyn/secrets.env` を操作するための主要インターフェイスです。変更を行うすべてのサブコマンドは、値を完全にマスクした P6 監査イベントを発行します。ファイルは常に `chmod 600` で書き込まれます。

ここに保存された値は reyn プロセスの起動時に `os.environ` にロードされ、任意の YAML フィールドの `${VAR}` 参照が自動的にその値に解決されます。詳細は [Reference: `reyn.yaml` — `${VAR}` interpolation](../config/reyn-yaml.md#var-interpolation) を参照してください。

## サブコマンド

### `set <KEY>[=<VALUE>]`

シークレットを書き込むか更新します。キーのみを指定した場合（`=VALUE` なし）、値は非表示入力（ターミナルエコーなし）でインタラクティブに読み取られます。

```bash
# インタラクティブ（非表示入力）
reyn secret set ANTHROPIC_API_KEY
# Value for ANTHROPIC_API_KEY: ****

# インラインの値（スクリプト / CI）
reyn secret set ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxx
```

キーがすでに存在する場合はその値が更新されます（他のキーの順序は保持されます）。新しいキーの場合は末尾に追加されます。

**出力：** `Secret '<KEY>' saved to ~/.reyn/secrets.env`

**監査イベント：** `secret_set` — ペイロード: `{key, value_masked: "***"}`

### `list`

`~/.reyn/secrets.env` に保存されたすべてのキーとそのステータスを表示します。値は**絶対に**表示されません。

```bash
reyn secret list
```

出力：

```
KEY                           STATUS
─────────────────────────────────────
ANTHROPIC_API_KEY             set
GITHUB_PERSONAL_ACCESS_TOKEN  set
OPENAI_API_KEY                stored (not yet in env)
```

| ステータス | 意味 |
|----------|------|
| `set` | キーは `secrets.env` にあり、現在 `os.environ` に存在します（起動時にロード済み）。 |
| `stored (not yet in env)` | キーは `secrets.env` にありますが、まだ `os.environ` にありません。キーが追加されてから reyn プロセスが再起動されていません。 |

シークレットが保存されていない場合: `No secrets stored in ~/.reyn/secrets.env`

### `clear <KEY>`

`~/.reyn/secrets.env` から単一のキーを削除します。冪等です。キーが存在しない場合は何も変更されず、エラーも返りません。

```bash
reyn secret clear GITHUB_PERSONAL_ACCESS_TOKEN
```

**出力（キーが見つかった場合）：** `Secret '<KEY>' removed from ~/.reyn/secrets.env`

**出力（キーが見つからなかった場合）：** `Secret '<KEY>' not found in ~/.reyn/secrets.env (nothing changed)`

**監査イベント（キーが見つかった場合）：** `secret_cleared` — ペイロード: `{key}`

### `rotate <KEY>[=<VALUE>]`

ローテーション意図を明示してシークレットを更新します。意味的には `set` と同一ですが、古い認証情報が置き換えられたことを監査消費者に通知するために、監査ログに `secret_rotated` を記録します。

```bash
# インタラクティブなローテーション（非表示入力）
reyn secret rotate ANTHROPIC_API_KEY

# インラインのローテーション
reyn secret rotate ANTHROPIC_API_KEY=sk-ant-new-xxxxxxxxxx
```

侵害または期限切れの認証情報を交換する場合は、監査証跡にローテーションイベントが明記されるよう `set` ではなく `rotate` を使用してください。

**監査イベント：** `secret_rotated` — ペイロード: `{key, value_masked: "***"}`

## 引数

| 引数 | コマンド | 説明 |
|-----|----------|------|
| `KEY` | `set`、`clear`、`rotate` | 環境変数名（例: `ANTHROPIC_API_KEY`）。空にはできません。 |
| `VALUE` | `set`、`rotate` | シークレットの値。省略した場合（引数に `=` がない場合）、値は非表示入力でインタラクティブにプロンプトされます。 |

## 例

### 新しいプロジェクトの初回セットアップ

```bash
# LLM キー
reyn secret set ANTHROPIC_API_KEY

# MCP サーバーの認証情報
reyn secret set GITHUB_PERSONAL_ACCESS_TOKEN

# 確認
reyn secret list
```

### CI / 非インタラクティブな使用

```bash
# インタラクティブプロンプトを避けるために値をインラインで渡す
reyn secret set ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}"
```

### 侵害されたトークンのローテーション

```bash
# 古い値を置き換え、監査ログに secret_rotated を記録
reyn secret rotate GITHUB_PERSONAL_ACCESS_TOKEN
```

### サーバーへのアクセス取り消し

```bash
# 認証情報を削除。次回呼び出し時にサーバーは失敗します（期待される動作）
reyn secret clear GITHUB_PERSONAL_ACCESS_TOKEN
```

## ファイル形式

`~/.reyn/secrets.env` は標準的な dotenv 形式のファイルです：

```
# コメントをサポート
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxxxxx
OPENAI_API_KEY=sk-xxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx

# 引用符付きの値をサポート
SLACK_BOT_TOKEN="xoxb-yyyyyyyy"
```

このファイルはテキストエディタで直接編集できます。`reyn secret` は便利なラッパーであり、管理する唯一の手段ではありません。ファイルは次の reyn プロセス起動時に再ロードされます。

## Exit codes

| コード | 意味 |
|------|---------|
| `0` | 成功。 |
| `1` | 無効な引数（空のキーなど）またはファイル書き込みの I/O エラー。 |

## 関連情報

- [コンセプト: シークレット管理](../../concepts/secret-handling.md) — メンタルモデル、セキュリティ特性、ロードタイミング
- [Reference: `reyn mcp`](mcp.md) — MCP サーバー固有の認証情報向け `set-secret` / `clear-secret`
- [Reference: `reyn.yaml`](../config/reyn-yaml.md#var-interpolation) — 設定ファイルでの `${VAR}` の使用
