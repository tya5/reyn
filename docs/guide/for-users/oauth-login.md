---
type: how-to
topic: using-reyn
audience: [human]
---

# Log in to an OAuth provider

Some tools need an OAuth login rather than a static API key — for example a
GitHub or Google integration that requires you to approve access in a browser.
`reyn auth` runs the [RFC 8628 device-grant flow](https://datatracker.ietf.org/doc/html/rfc8628):
you approve once in a browser and Reyn stores (and auto-refreshes) the token.

> Static API keys go through `reyn secret` instead — use `reyn auth` only for
> providers that require an interactive OAuth approval.

## 1. Declare the provider

Add the provider under `auth.providers` in `reyn.yaml`:

```yaml
# reyn.yaml
auth:
  providers:
    github:
      client_id: "${secret:github_oauth_client_id}"
      device_authorization_url: "https://github.com/login/device/code"
      token_url: "https://github.com/login/oauth/access_token"
      scopes: [repo, user]
      client_secret: "${secret:github_oauth_client_secret}"   # omit for PKCE-only clients
```

| Field | Required | |
|-------|----------|---|
| `client_id` | yes | OAuth client id from the provider |
| `device_authorization_url` | yes | Returns the device + user codes |
| `token_url` | yes | Issues the access / refresh tokens |
| `scopes` | yes | List of scopes (`[]` if none) |
| `client_secret` | no | Confidential clients only |
| `audience` | no | Required by some providers (e.g. Auth0) |

## 2. Store the secrets

`${secret:...}` values resolve from `~/.reyn/secrets.env`:

```bash
reyn secret set github_oauth_client_id
reyn secret set github_oauth_client_secret
```

## 3. Log in

```bash
reyn auth login github
```

Reyn prints a URL and a user code. Open the URL in any browser, enter the
code, approve — Reyn polls until you finish and then saves the token:

```
$ reyn auth login github

To authenticate, open this URL in your browser:
  https://github.com/login/device
  and enter code: WDJB-MJHT

Waiting for approval...
Saved OAuth token under key 'github'. Expires at 2026-05-17T11:00:00+00:00.
```

The token is written to `~/.reyn/oauth_tokens.json` (`chmod 600`) and refreshed
automatically when a skill uses it — you don't log in again until the refresh
token itself expires.

### Multiple accounts for one provider

Use `--save-as` to keep separate tokens under distinct keys:

```bash
reyn auth login github --save-as github-work
```

## Manage stored tokens

```bash
reyn auth list           # list saved token keys + expiry
reyn auth revoke github  # remove a token from the store
```

## See also

- [Reference: `reyn auth`](../../reference/cli/auth.md) — `login` / `list` / `revoke`, options, audit events
- [Reference: `reyn.yaml` — `auth` block](../../reference/config/reyn-yaml.md#auth-block) — full provider-field schema
- [Concepts: secret handling](../../concepts/runtime/secret-handling.md) — OAuth lifecycle and credential scoping
