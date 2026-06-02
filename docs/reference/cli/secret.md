---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn secret]
---

# `reyn secret`

Manage secrets stored in `~/.reyn/secrets.env`. See [Concepts: secret handling](../../concepts/runtime/secret-handling.md) for the mental model and security properties.

## Synopsis

```
reyn secret set <KEY>[=<VALUE>]
reyn secret list
reyn secret clear <KEY>
reyn secret rotate <KEY>[=<VALUE>]
```

## Description

`reyn secret` is the primary interface for `~/.reyn/secrets.env` — the universal secret store used by all reyn components. Every mutating subcommand emits a P6 audit event with the value fully masked. The file is always written with `chmod 600`.

Values stored here are loaded into `os.environ` at reyn process startup so that `${VAR}` references in any YAML field resolve to them automatically. See [Reference: `reyn.yaml` — `${VAR}` interpolation](../config/reyn-yaml.md#var-interpolation) for details.

## Subcommands

### `set <KEY>[=<VALUE>]`

Write or update a secret. If only the key is given (no `=VALUE`), the value is read interactively with hidden input (no terminal echo).

```bash
# Interactive (hidden input)
reyn secret set ANTHROPIC_API_KEY
# Value for ANTHROPIC_API_KEY: ****

# Inline value (scripting / CI)
reyn secret set ANTHROPIC_API_KEY=sk-ant-xxxxxxxxxx
```

If the key already exists, its value is updated in-place (order of other keys is preserved). If the key is new, it is appended.

**Output:** `Secret '<KEY>' saved to ~/.reyn/secrets.env`

**Audit event:** `secret_set` — payload: `{key, value_masked: "***"}`

### `list`

Show all keys stored in `~/.reyn/secrets.env` and their status. Values are **never** displayed.

```bash
reyn secret list
```

Output:

```
KEY                           STATUS
─────────────────────────────────────
ANTHROPIC_API_KEY             set
GITHUB_PERSONAL_ACCESS_TOKEN  set
OPENAI_API_KEY                stored (not yet in env)
```

| Status | Meaning |
|--------|---------|
| `set` | Key is in `secrets.env` and is currently in `os.environ` (loaded at startup). |
| `stored (not yet in env)` | Key is in `secrets.env` but not yet in `os.environ` — reyn process was not restarted since the key was added. |

If no secrets are stored: `No secrets stored in ~/.reyn/secrets.env`

### `clear <KEY>`

Remove a single key from `~/.reyn/secrets.env`. Idempotent — if the key is not present, nothing changes and no error is returned.

```bash
reyn secret clear GITHUB_PERSONAL_ACCESS_TOKEN
```

**Output (key found):** `Secret '<KEY>' removed from ~/.reyn/secrets.env`

**Output (key not found):** `Secret '<KEY>' not found in ~/.reyn/secrets.env (nothing changed)`

**Audit event (key found):** `secret_cleared` — payload: `{key}`

### `rotate <KEY>[=<VALUE>]`

Update a secret with explicit rotation intent. Semantically identical to `set` but records `secret_rotated` in the audit log, signalling to audit consumers that an old credential was superseded.

```bash
# Interactive rotation (hidden input)
reyn secret rotate ANTHROPIC_API_KEY

# Inline rotation
reyn secret rotate ANTHROPIC_API_KEY=sk-ant-new-xxxxxxxxxx
```

Use `rotate` (not `set`) when replacing a compromised or expired credential so the audit trail clearly marks the rotation event.

**Audit event:** `secret_rotated` — payload: `{key, value_masked: "***"}`

## Arguments

| Argument | Commands | Description |
|----------|----------|-------------|
| `KEY` | `set`, `clear`, `rotate` | Environment variable name (e.g. `ANTHROPIC_API_KEY`). Must be non-empty. |
| `VALUE` | `set`, `rotate` | Secret value. If omitted (no `=` in the argument), value is prompted interactively with hidden input. |

## Examples

### Initial setup for a new project

```bash
# LLM key
reyn secret set ANTHROPIC_API_KEY

# MCP server credential
reyn secret set GITHUB_PERSONAL_ACCESS_TOKEN

# Verify
reyn secret list
```

### CI / non-interactive use

```bash
# Pass value inline to avoid interactive prompt
reyn secret set ANTHROPIC_API_KEY="${ANTHROPIC_API_KEY}"
```

### Rotating a compromised token

```bash
# Replaces the old value; audit log records secret_rotated
reyn secret rotate GITHUB_PERSONAL_ACCESS_TOKEN
```

### Revoking access to a server

```bash
# Remove the credential; server will fail on next call (expected)
reyn secret clear GITHUB_PERSONAL_ACCESS_TOKEN
```

## File format

`~/.reyn/secrets.env` is a standard dotenv file:

```
# Comments are supported
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxxxxxx
OPENAI_API_KEY=sk-xxxxxxxx
ANTHROPIC_API_KEY=sk-ant-xxxxxxxx

# Quoted values are supported
SLACK_BOT_TOKEN="xoxb-yyyyyyyy"
```

You can edit this file directly in a text editor — `reyn secret` is a convenience wrapper, not the only way to manage it. The file is reloaded on the next reyn process start.

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success. |
| `1` | Invalid arguments (empty key, etc.) or I/O error writing the file. |

## See also

- [Concepts: secret handling](../../concepts/runtime/secret-handling.md) — mental model, security properties, load timing
- [Reference: `reyn mcp`](mcp.md) — `set-secret` / `clear-secret` for MCP-server-specific credentials
- [Reference: `reyn.yaml`](../config/reyn-yaml.md#var-interpolation) — using `${VAR}` in config files
