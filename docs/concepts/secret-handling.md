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

## See also

- [Reference: `reyn secret`](../reference/cli/secret.md) — full CLI syntax
- [Reference: `reyn mcp`](../reference/cli/mcp.md) — `set-secret` / `clear-secret` subcommands
- [Reference: `reyn.yaml`](../reference/config/reyn-yaml.md) — `${VAR}` interpolation in config fields
- [Concepts: permission model](permission-model.md) — `mcp_install` permission gating
- ADR-0030 `docs/deep-dives/decisions/0030-universal-secret-handling.md` — design rationale (implementation team, internal)
