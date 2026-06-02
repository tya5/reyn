---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn auth]
---

# `reyn auth`

RFC 8628 デバイス認可グラントフローを通じて OAuth トークンを管理します。トークンは `~/.reyn/oauth_tokens.json`（chmod 600）に保存され、スキルから使用される際に `reyn.secrets.get_valid_token` によって自動更新されます。

## 概要

```
reyn auth login <PROVIDER> [--save-as <KEY>]
reyn auth list
reyn auth revoke <KEY>
```

## 説明

`reyn auth` は対話的な承認ステップを必要とする OAuth 2.0 認証情報を管理します。静的 API キーを扱う `reyn secret` を補完するコマンドです。`reyn secret` は `~/.reyn/secrets.env` にフラットなキー=値のペアを書き込みますが、`reyn auth` は構造化されたトークンオブジェクト（アクセストークン、リフレッシュトークン、有効期限、スコープ）を `~/.reyn/oauth_tokens.json` に書き込みます。

変更を行うすべての操作は P6 監査イベントを発行します。トークンストアは常に `chmod 600` で書き込まれます。

## サブコマンド

### `login <PROVIDER>`

`<PROVIDER>` に対して RFC 8628 デバイス認可グラントフローを実行し、取得したトークンを保存します。

```
reyn auth login PROVIDER [--save-as KEY]
```

**フロー:**

1. プロバイダーの `device_authorization_url` に POST して `device_code`、`user_code`、`verification_uri` を受け取ります。
2. URL を表示します（サーバーが `verification_uri_complete` を返さない場合はコードも表示）。オペレーターは任意のデバイスでその URL にアクセスして承認します。
3. オペレーターが承認するかデバイスコードが期限切れになるまで、トークンエンドポイントをポーリングします（`slow_down` レスポンス時は RFC 8628 §3.5 のバックオフを適用）。
4. 成功すると、トークンをキーの下に `~/.reyn/oauth_tokens.json` へ保存し、有効期限を表示します。

**引数:**

| 引数 | 説明 |
|------|------|
| `PROVIDER` | `reyn.yaml` の `auth.providers.<name>` に定義されたプロバイダーキー（例: `github`、`google`）。 |

**オプション:**

| オプション | 説明 |
|-----------|------|
| `--save-as KEY` | プロバイダー名の代わりに指定したキーでトークンを保存します（デフォルト: `PROVIDER`）。同一プロバイダーで複数のアカウントを認証する場合に便利です。 |

**例 — GitHub ログイン:**

```bash
$ reyn auth login github

To authenticate, open this URL in your browser:
  https://github.com/login/device
  and enter code: WDJB-MJHT

Waiting for approval...
Saved OAuth token under key 'github'. Expires at 2026-05-17T11:00:00+00:00.
```

**例 — カスタムキーでログイン:**

```bash
$ reyn auth login github --save-as github-work
Saved OAuth token under key 'github-work'. Expires at 2026-05-17T11:00:00+00:00.
```

**発行される P6 監査イベント:**

| イベント | タイミング |
|---------|-----------|
| `oauth_login_started` | デバイスコード取得後。ペイロード: `key`、`provider`、`device_code`（末尾 4 文字のみ）、`verification_uri`、`expires_at`。 |
| `oauth_login_completed` | アクセストークン保存後。ペイロード: `key`、`expires_at`、`scopes`。 |

**Exit codes:**

| コード | 意味 |
|------|------|
| `0` | トークンの保存に成功。 |
| `1` | プロバイダーが `reyn.yaml` に設定されていない、またはフローが中断された（`KeyboardInterrupt`）。 |
| `2` | デバイスグラントの失敗（例: ユーザーが拒否、デバイスコードの期限切れ、プロバイダーエラー）。 |

### `list`

`~/.reyn/oauth_tokens.json` に保存されているすべての OAuth トークンキーをステータスおよび有効期限とともに一覧表示します。トークン値とスコープは**絶対に**表示されません。

```bash
$ reyn auth list
  github: valid, expires 2026-05-17T11:00:00+00:00
  github-work: near-expiry, expires 2026-05-16T09:05:00+00:00
```

60 秒以内に期限切れになるトークンは `near-expiry`、それ以外は `valid` と表示されます。ストア内の不正なエントリは `<malformed>` と表示されます。

ストアが空の場合:

```bash
$ reyn auth list
(no OAuth tokens stored)
```

**Exit codes:**

| コード | 意味 |
|------|------|
| `0` | 常に（ストアが空の場合も含む）。 |

### `revoke <KEY>`

`~/.reyn/oauth_tokens.json` からキーで指定したトークンを削除します。この操作はローカルのみで行われ、プロバイダーの失効エンドポイントは呼び出しません。

```bash
$ reyn auth revoke github
Revoked 'github' from local OAuth store.
```

キーが存在しない場合はエラーが表示され、コード 1 で終了します:

```bash
$ reyn auth revoke nonexistent
No token under key 'nonexistent'.
```

**引数:**

| 引数 | 説明 |
|------|------|
| `KEY` | 削除するトークンのキー（= トークン保存時に使用したキー）。 |

**Exit codes:**

| コード | 意味 |
|------|------|
| `0` | トークンの削除に成功。 |
| `1` | キーがストアに存在しない。 |

> **注意:** `revoke` は冪等ではありません。存在しないキーを指定するとコード 1 で終了します。スクリプトで冪等な削除が必要な場合は、事前に `reyn auth list` でキーの存在を確認してください。

## 設定

プロバイダーは `reyn.yaml` の `auth.providers.<name>` に設定します。各エントリは `OAuthProviderConfig` にマッピングされます:

| フィールド | 必須 | 説明 |
|-----------|------|------|
| `client_id` | はい | プロバイダーから発行された OAuth クライアント ID。 |
| `device_authorization_url` | はい | RFC 8628 デバイス認可エンドポイントの URL。 |
| `token_url` | はい | RFC 6749 トークンエンドポイントの URL（ポーリングとリフレッシュの両方で使用）。 |
| `scopes` | いいえ | OAuth スコープ文字列のリスト（デフォルト: `[]`）。 |
| `client_secret` | いいえ | 公開クライアント（インストール型アプリ）では省略可。 |
| `audience` | いいえ | API オーディエンス識別子（Auth0 等のプロバイダー向け）。 |

完全なスキーマと `${VAR}` 補間については [Reference: `reyn.yaml`](../config/reyn-yaml.md) を参照してください。

## トークンストア

`~/.reyn/oauth_tokens.json` はトークン名をキーとする JSON オブジェクトです:

```json
{
  "github": {
    "access_token": "...",
    "refresh_token": "...",
    "token_uri": "https://github.com/login/oauth/access_token",
    "client_id": "Iv1.xxxxxxxx",
    "expires_at": "2026-05-17T11:00:00+00:00",
    "scopes": ["repo", "user"],
    "client_secret": null
  }
}
```

ファイルは常に `chmod 600` で書き込まれます。読み込み時にグループや他のユーザーから読み取り可能な状態になっている場合、Reyn は自動的にパーミッションを修正し、警告を発します。

`REYN_OAUTH_TOKENS_PATH` 環境変数でストアのパスを上書きできます（テスト用途に便利です）。

## トークンの自動更新

`reyn.secrets.get_valid_token(key)` を呼び出すスキルは、有効なアクセストークンを自動的に受け取ります。トークンの有効期限まで 60 秒以内の場合、Reyn は返却前に RFC 6749 §6 のリフレッシュ POST を発行します。リフレッシュに失敗した場合（例: リフレッシュトークンの失効）、Reyn は `token_refresh_failed` を発行し、`re_auth_required=True` を持つ `OAuthRefreshError` を送出します。この場合、オペレーターは `reyn auth login <provider>` を再実行する必要があります。

## 関連情報

- [Reference: `reyn secret`](secret.md) — 静的な dotenv シークレットの管理
- [コンセプト: シークレット管理](../../concepts/runtime/secret-handling.md) — OAuth ライフサイクルと認証情報のスコープ
- [Reference: `reyn.yaml`](../config/reyn-yaml.md) — `auth:` 設定ブロック
- [Reference: Events](../runtime/events.md) — `oauth_login_started`、`oauth_login_completed`、`token_refreshed`、`token_refresh_failed`
