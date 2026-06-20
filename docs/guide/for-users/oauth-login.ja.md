---
type: how-to
topic: using-reyn
audience: [human]
---

# OAuth プロバイダにログインする

ツールによっては静的 API キーではなく OAuth ログインが必要です — 例えば
ブラウザでアクセス承認が要る GitHub や Google 連携など。`reyn auth` は
[RFC 8628 デバイスグラントフロー](https://datatracker.ietf.org/doc/html/rfc8628)
を実行します: ブラウザで一度承認すれば、Reyn がトークンを保存（かつ自動更新）します。

> 静的 API キーは代わりに `reyn secret` を使います。`reyn auth` は対話的な
> OAuth 承認が必要なプロバイダにのみ使用してください。

## 1. プロバイダを宣言する

`reyn.yaml` の `auth.providers` にプロバイダを追加:

```yaml
# reyn.yaml
auth:
  providers:
    github:
      client_id: "${secret:github_oauth_client_id}"
      device_authorization_url: "https://github.com/login/device/code"
      token_url: "https://github.com/login/oauth/access_token"
      scopes: [repo, user]
      client_secret: "${secret:github_oauth_client_secret}"   # PKCE-only クライアントでは省略
```

| フィールド | 必須 | |
|-----------|------|---|
| `client_id` | はい | プロバイダ発行の OAuth クライアント id |
| `device_authorization_url` | はい | device + user コードを返す |
| `token_url` | はい | アクセス/リフレッシュトークンを発行 |
| `scopes` | はい | スコープのリスト（無ければ `[]`） |
| `client_secret` | いいえ | 機密クライアントのみ |
| `audience` | いいえ | 一部プロバイダ（例: Auth0）で必須 |

## 2. シークレットを保存する

`${secret:...}` の値は `~/.reyn/secrets.env` から解決されます:

```bash
reyn secret set github_oauth_client_id
reyn secret set github_oauth_client_secret
```

## 3. ログインする

```bash
reyn auth login github
```

Reyn が URL と user コードを表示します。任意のブラウザで URL を開き、コードを
入力して承認すると、Reyn は完了までポーリングしトークンを保存します:

```
$ reyn auth login github

To authenticate, open this URL in your browser:
  https://github.com/login/device
  and enter code: WDJB-MJHT

Waiting for approval...
Saved OAuth token under key 'github'. Expires at 2026-05-17T11:00:00+00:00.
```

トークンは `~/.reyn/oauth_tokens.json`（`chmod 600`）に書き込まれ、skill が
使用する際に自動更新されます — リフレッシュトークン自体が失効するまで
再ログインは不要です。

### 1 プロバイダで複数アカウント

`--save-as` で別キーにトークンを分けて保存:

```bash
reyn auth login github --save-as github-work
```

## 保存済みトークンを管理する

```bash
reyn auth list           # 保存済みトークンキー + 有効期限を一覧
reyn auth revoke github  # ストアからトークンを削除
```

## 関連

- [リファレンス: `reyn auth`](../../reference/cli/auth.md) — `login` / `list` / `revoke`、オプション、監査イベント
- [リファレンス: `reyn.yaml` — `auth` ブロック](../../reference/config/reyn-yaml.md#auth-block) — 全プロバイダフィールドスキーマ
- [コンセプト: secret handling](../../concepts/runtime/secret-handling.md) — OAuth ライフサイクルと認証情報スコープ
