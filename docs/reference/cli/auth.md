---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn auth]
---

# `reyn auth`

Manage OAuth tokens via the RFC 8628 Device Authorization Grant flow (FP-0016 Component C). Tokens are stored in `~/.reyn/oauth_tokens.json` (chmod 600) and automatically refreshed via `reyn.secrets.get_valid_token` when used by skills (FP-0016 Component B).

## Synopsis

```
reyn auth login <PROVIDER> [--save-as <KEY>]
reyn auth list
reyn auth revoke <KEY>
```

## Description

`reyn auth` manages OAuth 2.0 credentials that require an interactive approval step. It complements `reyn secret`, which handles static API keys: `reyn secret` writes flat key=value pairs to `~/.reyn/secrets.env`; `reyn auth` writes structured token objects (access token, refresh token, expiry, scopes) to `~/.reyn/oauth_tokens.json`.

All mutating operations emit P6 audit events. The token store is always written with `chmod 600`.

## Subcommands

### `login <PROVIDER>`

Run the RFC 8628 Device Authorization Grant flow for `<PROVIDER>` and persist the resulting token.

```
reyn auth login PROVIDER [--save-as KEY]
```

**Flow:**

1. POSTs to the provider's `device_authorization_url` and receives a `device_code`, `user_code`, and `verification_uri`.
2. Prints the URL (and code if `verification_uri_complete` is not provided by the server); the operator visits the URL on any device and approves.
3. Polls the token endpoint (with RFC 8628 §3.5 backoff on `slow_down` responses) until the operator approves or the device code expires.
4. On success, saves the token to `~/.reyn/oauth_tokens.json` under the key and prints the expiry time.

**Arguments:**

| Argument | Description |
|----------|-------------|
| `PROVIDER` | Provider key from `reyn.yaml` `auth.providers.<name>` (e.g. `github`, `google`). |

**Options:**

| Option | Description |
|--------|-------------|
| `--save-as KEY` | Store the resulting token under this key instead of the provider name (default: `PROVIDER`). Useful when authenticating as multiple accounts for the same provider. |

**Example — GitHub login:**

```bash
$ reyn auth login github

To authenticate, open this URL in your browser:
  https://github.com/login/device
  and enter code: WDJB-MJHT

Waiting for approval...
Saved OAuth token under key 'github'. Expires at 2026-05-17T11:00:00+00:00.
```

**Example — login with a custom key:**

```bash
$ reyn auth login github --save-as github-work
Saved OAuth token under key 'github-work'. Expires at 2026-05-17T11:00:00+00:00.
```

**P6 audit events emitted:**

| Event | When |
|-------|------|
| `oauth_login_started` | After the device code is obtained. Payload: `key`, `provider`, `device_code` (last 4 chars only), `verification_uri`, `expires_at`. |
| `oauth_login_completed` | After the access token is saved. Payload: `key`, `expires_at`, `scopes`. |

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Token saved successfully. |
| `1` | Provider not configured in `reyn.yaml`, or flow interrupted (`KeyboardInterrupt`). |
| `2` | Device grant failed (e.g. user denied, device code expired, provider error). |

### `list`

List all OAuth token keys currently in `~/.reyn/oauth_tokens.json`, with status and expiry. Token values and scopes are never printed.

```bash
$ reyn auth list
  github: valid, expires 2026-05-17T11:00:00+00:00
  github-work: near-expiry, expires 2026-05-16T09:05:00+00:00
```

Tokens expiring within 60 seconds are shown as `near-expiry`; all others are `valid`. Malformed entries in the store are shown as `<malformed>`.

If the store is empty:

```bash
$ reyn auth list
(no OAuth tokens stored)
```

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Always (even when the store is empty). |

### `revoke <KEY>`

Remove a token from `~/.reyn/oauth_tokens.json` by key. This operation is local only — it does not call the provider's revocation endpoint.

```bash
$ reyn auth revoke github
Revoked 'github' from local OAuth store.
```

If the key is not present, an error is printed and the command exits with code 1:

```bash
$ reyn auth revoke nonexistent
No token under key 'nonexistent'.
```

**Arguments:**

| Argument | Description |
|----------|-------------|
| `KEY` | The token key to remove (= the key under which the token was saved). |

**Exit codes:**

| Code | Meaning |
|------|---------|
| `0` | Token removed. |
| `1` | Key not found in the store. |

> **Note:** `revoke` is not idempotent — calling it for an absent key exits with code 1. Use `reyn auth list` first to confirm the key exists if you need idempotent removal in scripts.

## Configuration

Providers are configured under `auth.providers.<name>` in `reyn.yaml`. Each entry maps to an `OAuthProviderConfig`:

| Field | Required | Description |
|-------|----------|-------------|
| `client_id` | yes | OAuth client ID issued by the provider. |
| `device_authorization_url` | yes | RFC 8628 device authorization endpoint URL. |
| `token_url` | yes | RFC 6749 token endpoint URL (used for both polling and refresh). |
| `scopes` | no | List of OAuth scope strings (default: `[]`). |
| `client_secret` | no | Omit for public (installed-app) clients. |
| `audience` | no | API audience identifier (Auth0 and similar providers). |

See [Reference: `reyn.yaml`](../config/reyn-yaml.md) for the full schema and `${VAR}` interpolation.

## Token store

`~/.reyn/oauth_tokens.json` is a JSON object keyed by token name:

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

The file is always written with `chmod 600`. If the file is group- or world-readable when read, Reyn auto-fixes permissions and emits a warning.

You can override the store path with the `REYN_OAUTH_TOKENS_PATH` environment variable (useful for testing).

## Automatic token refresh

Skills that call `reyn.secrets.get_valid_token(key)` (FP-0016 Component B) receive a valid access token automatically. If the token is within 60 seconds of expiry, Reyn issues a RFC 6749 §6 refresh POST before returning. On refresh failure (e.g. revoked refresh token), Reyn emits `token_refresh_failed` and raises `OAuthRefreshError` with `re_auth_required=True` — the operator must run `reyn auth login <provider>` again.

## See also

- [Reference: `reyn secret`](secret.md) — managing static dotenv secrets
- [Concepts: secret handling](../../concepts/secret-handling.md) — OAuth lifecycle and credential scoping
- [Reference: `reyn.yaml`](../config/reyn-yaml.md) — `auth:` configuration block
- [Reference: Events](../runtime/events.md) — `oauth_login_started`, `oauth_login_completed`, `token_refreshed`, `token_refresh_failed`
