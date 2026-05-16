---
type: concept
topic: security
audience: [human, agent]
---

# Secret handling

reyn uses a single, universal mechanism for secrets across every component: MCP server credentials, LLM API keys, web-server TLS certificates, and any future integrations all read from the same place.

The mental model is: **secrets live in `~/.reyn/secrets.env`; config files reference them as `${VAR}`; all reyn components see them via `os.environ`.**

## Where secrets live

```
~/.reyn/secrets.env        # chmod 600 — the one place for all secrets
```

The file is a standard [dotenv](https://github.com/motdotla/dotenv) format — one `KEY=value` pair per line, `#` comments supported:

```
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxxxxxxxxx
OPENAI_API_KEY=sk-xxxxxxxxxxxxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxxxxxxxxxx
LITELLM_PROXY_TOKEN=Bearer sk-my-proxy-token
```

**Security properties:**

- Created with `chmod 600` on first write; reyn auto-corrects wider permissions on startup with a warning.
- Never checked into git — the file lives in `~/.reyn/`, outside any project root.
- Never printed by reyn commands — `reyn secret list` shows key names and status only, not values.
- Subprocess-inherited (intentional): MCP servers and Python preprocessors started by reyn see the same environment. Trace dumps (`REYN_LLM_TRACE_DUMP`) redact known secret patterns.

## `${VAR}` interpolation

Any string field in any reyn YAML file can reference an environment variable using `${VAR}` syntax. The variable is resolved from `os.environ` at startup (after `secrets.env` is loaded), so values in `~/.reyn/secrets.env` are available everywhere:

```yaml
# reyn.yaml — all ${VAR} references below are resolved from secrets.env or shell env
models:
  default-sonnet:
    model: claude-sonnet-4-5
    api_key: ${ANTHROPIC_API_KEY}          # LLM API key
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

Resolution rules:

- `${VAR}` — expands to the env var value; emits a warning and expands to `""` if undefined (never a hard error).
- `$$` — literal `$` sign (escape).
- All string fields in all YAML sections are scanned recursively, including nested dicts and lists.
- Shell environment variables take priority over `secrets.env` values (so you can always override from the shell for a single run).

## Load timing

reyn loads `~/.reyn/secrets.env` once at process startup, before any component initializes. This means:

- All reyn components (`LiteLLMClient`, MCP `expand_env()`, web server, etc.) see secret values via the standard `os.environ.get()` without any knowledge of `secrets.env`.
- YAML `${VAR}` interpolation resolves against the already-loaded environment.
- To pick up a changed secret, restart the reyn process. (A `reyn secret reload` command for zero-restart rotation is a planned phase 2 addition.)

**Load failure policy:** if `secrets.env` is absent, startup continues silently. If the file exists but has parse errors, reyn emits a warning per bad line and skips it — it does not abort startup.

## `reyn secret` CLI

The `reyn secret` subcommand is the primary way to manage `~/.reyn/secrets.env`. See [Reference: `reyn secret`](../reference/cli/secret.md) for full syntax. Typical flows:

### First-time setup

```bash
# Add your LLM API key
reyn secret set ANTHROPIC_API_KEY

# Value for ANTHROPIC_API_KEY: ****   ← hidden input
# Secret 'ANTHROPIC_API_KEY' saved to ~/.reyn/secrets.env

# Verify it's present (value never displayed)
reyn secret list
```

Output of `list`:

```
KEY                           STATUS
─────────────────────────────────────
ANTHROPIC_API_KEY             set
GITHUB_PERSONAL_ACCESS_TOKEN  set
OPENAI_API_KEY                stored (not yet in env)
```

### Inline value (scripting / CI)

```bash
reyn secret set ANTHROPIC_API_KEY=sk-ant-xxxxx
```

### Rotation

```bash
reyn secret rotate ANTHROPIC_API_KEY
# Value for ANTHROPIC_API_KEY: ****   ← new value, hidden input
# Secret 'ANTHROPIC_API_KEY' rotated in ~/.reyn/secrets.env
```

`rotate` is semantically identical to `set` but records `secret_rotated` in the audit log, signalling to audit consumers that an old value was superseded.

### Removal

```bash
reyn secret clear GITHUB_PERSONAL_ACCESS_TOKEN
```

### MCP-aware shortcut

When installing an MCP server, reyn prompts for required credentials automatically and stores them via the same mechanism:

```bash
reyn mcp install github
# github requires GITHUB_PERSONAL_ACCESS_TOKEN.
# How to obtain one: https://github.com/settings/personal-access-tokens/new
# GITHUB_PERSONAL_ACCESS_TOKEN: ****
# ✓ github added.
```

For an already-installed server, add or rotate a credential with:

```bash
reyn mcp set-secret github GITHUB_PERSONAL_ACCESS_TOKEN
```

This is a thin wrapper over `reyn secret set` that reads the server's env declarations and suggests the right key name.

## Audit log

Every mutating `reyn secret` command emits a P6 audit event. Values are fully masked in the event payload:

| Event | Trigger | Payload |
|-------|---------|---------|
| `secret_set` | `reyn secret set` | `key`, `value_masked: "***"` |
| `secret_cleared` | `reyn secret clear` | `key` |
| `secret_rotated` | `reyn secret rotate` | `key`, `value_masked: "***"` |

Filter for them with:

```bash
grep '"secret_' .reyn/events.jsonl
```

## Security model

**What `~/.reyn/secrets.env` protects against:**

- Accidental VCS commit of credentials (file is outside any project root).
- Group or world read access (reyn auto-corrects to 600 with a warning).
- Accidental display in CLI output (all commands mask values).

**What it does not protect against:**

- A compromised user account on the same machine (the file is still readable by that user).
- Processes running as the same user — they inherit the environment and can read `os.environ`.
- Reyn is not a vault or a HSM. For enterprise secret management (HashiCorp Vault, AWS Secrets Manager, macOS Keychain), use those systems to populate the shell environment before starting reyn — the `${VAR}` interpolation then picks up their values transparently.

## Relationship to config file scope tiers

`~/.reyn/secrets.env` is user-global — it applies across all projects on the machine. There is no project-scoped `secrets.env` in the current release; the intended pattern is:

- Project-specific secret **keys** are declared as `${VAR}` references in the project's `reyn.yaml` (checked into git, contains no actual values).
- Secret **values** live in `~/.reyn/secrets.env` (user-global, never in git).
- Per-machine overrides use `reyn.local.yaml` for non-secret config; `~/.reyn/secrets.env` for secret values.

## OAuth token lifecycle (FP-0016 B)

The static dotenv path (`~/.reyn/secrets.env`, chmod 600) is designed for **rotating API keys** that are set manually. Tokens that auto-refresh require a separate mechanism.

reyn ships an `OAuthToken` value type stored in `~/.reyn/oauth_tokens.json` (chmod 600). Each entry holds the access token, refresh token, expiry timestamp, and token endpoint URL.

**Runtime API** — skills access OAuth tokens via:

```python
from reyn.secrets import get_valid_token
token = await get_valid_token("github_oauth")
```

`get_valid_token(key)` behaviour:

- If the token expires within **60 seconds**, it refreshes via RFC 6749 §6 (refresh token grant) before returning.
- On successful refresh: persists the new token to `~/.reyn/oauth_tokens.json`, emits a `token_refreshed` P6 event, returns the fresh `access_token`.
- On refresh failure: emits `token_refresh_failed` and raises `OAuthRefreshError` — callers catch and surface to the operator.
- Concurrent refresh attempts for the same key are serialised with a per-key `asyncio.Lock` (avoids double-refresh races).

**Summary of P6 events emitted:**

| Event | Trigger |
|-------|---------|
| `token_refreshed` | Successful refresh; payload includes `key`, masked token hint |
| `token_refresh_failed` | Refresh request failed; payload includes `key`, `error` |

## Per-skill credential scoping (FP-0016 D)

**Threat model:** sub-skills that process untrusted documents could be prompt-injected into exfiltrating the parent skill's full secret store (Confused Deputy attack). Scoping prevents this.

### Declaration

Each skill declares `required_credentials` in its `skill.md` frontmatter:

```yaml
# skill.md frontmatter
---
name: pr-reviewer
required_credentials:
  - github_token
---
```

Accepted values:

| Value | Meaning |
|-------|---------|
| `[]` | No credentials needed (default for stdlib skills) |
| `["github_token", "openai_key"]` | Explicit allowlist |
| `["*"]` | Full delegation — backward-compat default when field is omitted |

### Enforcement

At `run_skill` boundaries the OS constructs a `ScopedSecretStore(allowed_keys=...)` and **intersects** it with the parent's scope (parent-cap semantics — a sub-skill can never have wider access than its parent).

Reads outside the allowed set raise `CredentialScopeError` (a `PermissionError` subclass).

Every scope decision emits a `sub_skill_credential_scope` P6 event for audit:

```json
{"skill": "<name>", "allowed_keys": ["github_token"]}
```

**Cross-references:**

- [Concepts: permission model](permission-model.md) "Per-skill credential scoping" — deeper detail including capability inheritance rules.
- [Reference: `skill.md` DSL](../reference/dsl/skill-md.md) — full `required_credentials` field reference.

## Device authorization grant (FP-0016 C)

For OAuth flows that require a browser redirect (unusable in headless agent contexts), reyn implements RFC 8628 Device Authorization Grant via:

```bash
reyn auth login <provider>
```

The command prints a user code and verification URL, polls the token endpoint, and stores the resulting tokens in `~/.reyn/oauth_tokens.json` for use by `get_valid_token`. No browser automation or callback server is required — the operator opens the URL and approves on their own device.

- Full CLI usage: [Reference: `reyn auth`](../reference/cli/auth.md)
- Provider configuration (`oauth.providers`): [Reference: `reyn.yaml`](../reference/config/reyn-yaml.md)

## Agent identity (FP-0016 E)

Every P6 event and every outbound HTTP call (MCP, future A2A) carries the agent identity. The identity is the `agent.id` field from `reyn.yaml`; when omitted it defaults to `reyn/<hostname>`.

The identity appears in:

- Event payloads as `agent_id`.
- Outbound HTTP requests as the `X-Reyn-Agent-Id` header.
- A2A task envelopes as the `initiator` field.

For cross-agent tracing and multi-agent topology: [Concepts: multi-agent](multi-agent.md) "Agent ID propagation".

## See also

- [Reference: `reyn secret`](../reference/cli/secret.md) — full CLI syntax
- [Reference: `reyn mcp`](../reference/cli/mcp.md) — `set-secret` / `clear-secret` subcommands
- [Reference: `reyn.yaml`](../reference/config/reyn-yaml.md) — `${VAR}` interpolation in config fields; OAuth provider config
- [Reference: `reyn auth`](../reference/cli/auth.md) — device authorization grant CLI
- [Reference: `skill.md` DSL](../reference/dsl/skill-md.md) — `required_credentials` field reference
- [Concepts: permission model](permission-model.md) — `mcp_install` permission gating; per-skill credential scoping
- [Concepts: multi-agent](multi-agent.md) — agent ID propagation
- ADR-0030 `docs/deep-dives/decisions/0030-universal-secret-handling.md` — design rationale (implementation team, internal)
