---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn mcp]
---

# `reyn mcp`

Manage MCP server configuration and expose Reyn agents to external MCP-aware clients.

## Synopsis

```
reyn mcp serve     [--project PATH] [--timeout SECONDS] [common flags]
reyn mcp search    <QUERY>
reyn mcp install   <SERVER_ID> [--scope SCOPE] [--env KEY=VALUE ...] [--non-interactive]
reyn mcp list      [--probe]
reyn mcp remove    <NAME> [--scope SCOPE]
reyn mcp set-secret <SERVER> <KEY>[=<VALUE>]
reyn mcp clear-secret <SERVER> [<KEY>]
```

## Overview

`reyn mcp` groups two distinct sets of operations under one command:

- **Outbound server management** — `search`, `install`, `list`, `remove`, `set-secret`, `clear-secret` manage the MCP servers that reyn calls as a client.
- **Inbound server mode** — `serve` exposes reyn's own agents to external MCP clients.

For the conceptual model and the Two roles framing, see [Concepts: MCP](../../concepts/mcp.md).

---

## Subcommand: `search`

Search the MCP server registry for available servers.

```
reyn mcp search <QUERY>
```

Queries the MCP registry API (`registry.modelcontextprotocol.io`) with a local cache (`~/.reyn/registry-cache/`, TTL 24h) for offline resilience.

```bash
reyn mcp search "github"
reyn mcp search "filesystem"
reyn mcp search "ファイル操作"
```

**Output:** tabular listing of matching servers including name, description, runtime hint, and the install command preview. Use the server identifier from this output with `reyn mcp install`.

---

## Subcommand: `install`

Install an MCP server into reyn's configuration.

```
reyn mcp install <SERVER_ID> [--scope SCOPE] [--env KEY=VALUE ...] [--non-interactive]
reyn mcp install --source <SOURCE_SPEC> [--scope SCOPE] [--env KEY=VALUE ...] [--non-interactive]
```

`install` is the recommended first step for any new MCP server. Two paths:

**Registry path** (default — pass a `<SERVER_ID>`):

1. Fetches the server's `server.json` from the registry (`registry.modelcontextprotocol.io`).
2. Checks that the required runtime (`npx`, `uvx`, `docker`, etc.) is installed.
3. Applies the `mcp_install` permission gate (see [permission interaction](#permission-interaction-mcp_install)).
4. Prompts for any required credentials (marked `isSecret` in the registry manifest) — or reads them from `--env` flags.
5. Stores credential values in `~/.reyn/secrets.env` (see [Concepts: secret handling](../../concepts/secret-handling.md)).
6. Writes the `mcp.servers.<name>` entry to the target scope config file, with `${VAR}` references for any secrets.
7. Emits a `mcp_server_installed` audit event.

**Source path** (`--source <SOURCE_SPEC>` — for servers not in the registry, including Anthropic's official reference servers like `@modelcontextprotocol/server-filesystem`):

Skips the registry fetch entirely and resolves the install metadata from the source specifier. Permission gate, credentials, config write, and audit event are identical to the registry path.

Supported source schemes:

| Scheme | Example | Resolves to |
|--------|---------|-------------|
| `npm:<package>[@version]` | `npm:@modelcontextprotocol/server-filesystem` | `command: npx, args: ["-y", "<package>"]` |
| `pypi:<package>[==version]` | `pypi:mcp-server-fetch` | `command: uvx, args: ["<package>"]` |
| `docker:<image>[:tag]` | `docker:mcp/playwright:latest` | `command: docker, args: ["run", "--rm", "-i", "<image>"]` |
| `https://github.com/<owner>/<repo>[/...]` | `https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem` | Heuristic: known repos resolve to `@scope/<package>` npm packages; unknown repos write the config without a `command`, surfacing as a clear failure at runtime rather than a silent bad install. |

`<SERVER_ID>` and `--source` are mutually exclusive.

### Options

| Flag | Default | Description |
|------|---------|-------------|
| `--scope SCOPE` | `local` | Config scope to write into: `local` (`.reyn/config.yaml`, gitignored), `project` (`reyn.yaml`, committed), or `user` (`~/.reyn/config.yaml`). |
| `--source <SPEC>` | — | Install from a direct source specifier (`npm:`, `pypi:`, `docker:`, or `https://github.com/...`) instead of from the registry. Mutually exclusive with `<SERVER_ID>`. |
| `--env KEY=VALUE` | — | Pre-supply an environment variable (may be repeated). Suppresses the interactive prompt for that key. |
| `--non-interactive` | off | Suppress all interactive prompts. Exit non-zero if required credentials are missing. For CI use. |

### Scope guidance

| Scope | Use case |
|-------|----------|
| `local` (default) | Personal / experimental — try a server without affecting teammates. |
| `project` | Team-wide — all team members get the server. Secrets remain as `${VAR}` references in committed config; actual values stay in each developer's `~/.reyn/secrets.env`. |
| `user` | Cross-project — servers you want available in all your projects (e.g. `filesystem`). |

### Examples

```bash
# Discover available servers
reyn mcp search "github"

# Install with interactive credential prompt
reyn mcp install io.github.modelcontextprotocol/server-github

# Install with credential supplied inline (CI)
reyn mcp install io.github.modelcontextprotocol/server-github \
  --env GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxxx \
  --non-interactive

# Install into project scope (team-shared config)
reyn mcp install io.github.modelcontextprotocol/server-github --scope project

# Install Anthropic official server (not in registry) via npm source
reyn mcp install --source npm:@modelcontextprotocol/server-filesystem

# Install via PyPI source
reyn mcp install --source pypi:mcp-server-fetch

# Install via Docker source
reyn mcp install --source docker:mcp/playwright

# Install via GitHub URL (heuristic resolver)
reyn mcp install --source https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem
```

### Permission interaction: `mcp_install`

Before writing anything to disk, `install` checks the `mcp_install` permission gate (ADR-0029). The default behaviour is `ask` — a prompt appears on first install:

```
[approval] install MCP server 'io.github.modelcontextprotocol/server-github'?

  [y] allow this install only
  [j] persist approval for this server
  [r] allow all future installs
  [N] deny
```

Enterprise teams can set `permissions.mcp_install: deny` in `reyn.yaml` to prevent any server additions, or `allow` to skip the prompt entirely. See [Concepts: permission model — `mcp_install`](../../concepts/permission-model.md#mcp_install-permission) for full details.

---

## Subcommand: `list`

List configured MCP servers and their status.

```
reyn mcp list [--probe]
```

By default reads from the config files only (no network calls, no subprocess launch):

```
NAME         TRANSPORT  STATUS         CREDENTIALS
filesystem   stdio      ready          (none)
github       stdio      ready          GITHUB_PERSONAL_ACCESS_TOKEN ✓ (set)
slack        stdio      missing-cred   SLACK_BOT_TOKEN ✗ (not set)
```

| Flag | Description |
|------|-------------|
| `--probe` | Handshake with each server to verify liveness. Slow — triggers actual subprocess launches and network calls. Adds `mcp_probe_called` audit events. |

---

## Subcommand: `remove`

Remove an MCP server from configuration.

```
reyn mcp remove <NAME> [--scope SCOPE]
```

Deletes the `mcp.servers.<name>` entry from the specified (or inferred) scope config file. Does **not** touch `~/.reyn/secrets.env` — the credential remains available for other servers that may use the same key.

| Flag | Default | Description |
|------|---------|-------------|
| `--scope SCOPE` | auto-detect | Scope tier to remove from. If omitted, removes from whichever scope the server appears in (local first, then project, then user). |

Note: any currently running `reyn chat` subprocess that has already connected to the server continues until the session ends. The change takes effect on the next reyn process start.

---

## Subcommand: `set-secret`

Set a credential for a configured MCP server.

```
reyn mcp set-secret <SERVER> <KEY>[=<VALUE>]
```

`set-secret` is a MCP-aware thin wrapper over `reyn secret set`. It reads the server's `mcp.servers.<name>.env` declarations (or the registry `server.json`) to suggest the correct key name, then stores the value in `~/.reyn/secrets.env` via the universal secret store.

Use `set-secret` to:

- Add a credential that was skipped during `install`.
- Rotate an existing credential for a specific server.

```bash
# Interactive (hidden input)
reyn mcp set-secret github GITHUB_PERSONAL_ACCESS_TOKEN

# Inline value
reyn mcp set-secret github GITHUB_PERSONAL_ACCESS_TOKEN=ghp_new_token
```

Storage is universal — `reyn secret set GITHUB_PERSONAL_ACCESS_TOKEN=...` produces the same result.

---

## Subcommand: `clear-secret`

Remove a credential for a configured MCP server.

```
reyn mcp clear-secret <SERVER> [<KEY>]
```

| Argument | Description |
|----------|-------------|
| `SERVER` | Server name as declared in `mcp.servers.*`. |
| `KEY` | Secret key to remove. If omitted, clears **all** secrets declared for the server. |

```bash
# Clear a specific credential
reyn mcp clear-secret github GITHUB_PERSONAL_ACCESS_TOKEN

# Clear all credentials for a server
reyn mcp clear-secret slack
```

---

## Subcommand: `serve`

Expose Reyn agents to external MCP-aware clients over a JSON-RPC stdio transport.

```
reyn mcp serve [--project PATH] [--timeout SECONDS] [common flags]
```

`reyn mcp serve` launches Reyn as an MCP (Model Context Protocol) JSON-RPC server. External MCP-aware clients — Claude Code, Cursor, OpenAI Agents SDK with MCP enabled, and any other client that speaks the MCP protocol — can then submit messages to your Reyn agents using two tools: `list_agents` and `send_to_agent`.

This is the inverse of Reyn's MCP-client role (where Reyn calls out to third-party MCP servers). Here, the external client calls INTO Reyn. The same `reyn.yaml` and agent registry that `reyn chat` uses backs the MCP server — permissions are checked, events are emitted, and all normal OS validation runs.

For the conceptual model and the Two roles framing, see [Concepts: MCP — Role 2](../../concepts/mcp.md#role-2-mcp-server-external-clients-call-reyn).

`reyn mcp serve` starts a JSON-RPC server that speaks over stdio. There is no port; the MCP client launches the process itself and owns the transport. Because MCP clients (Claude Desktop, Cursor, Claude Code) typically spawn the server process with `cwd=/`, always pass `--project` in the client config's `args` list — the server has no other way to locate your `reyn.yaml`.

On startup the server:

1. Reads `reyn.yaml` from the project root and loads the agent registry.
2. Replays the WAL into per-agent snapshots so any stranded in-flight skills resume cleanly (same behavior as `reyn chat` startup).
3. Enters the MCP JSON-RPC loop and waits for tool calls.

On EOF from stdin (= the MCP client disconnects), the registry is shut down cleanly and all in-flight sessions drain.

The server runs non-interactively. No human is at the stdin that the MCP transport owns, so interactive permission prompts would block indefinitely. Pre-approve skill permissions in `reyn.yaml` with `permissions: allow` before wiring up the server.

### `serve` options

| Flag | Default | Description |
|------|---------|-------------|
| `--project PATH` | Closest ancestor directory containing `reyn.yaml`, else fails with exit code 1 | Project root. Required in most client configs because MCP clients ignore the `cwd` field when spawning the server process. |
| `--timeout SECONDS` | `60.0` | Maximum blocking time per `send_to_agent` call. On timeout the call returns whatever reply has accumulated; the agent keeps working in the background. The next `send_to_agent` call will receive the rest. |

Inherited common flags (`--model`, `--output-language`, `--max-phase-visits`, etc.) are accepted; see [common-flags.md](common-flags.md).

### Tools exposed

Two MCP tools are registered under the server name `reyn`:

#### `list_agents()`

Returns a JSON array of objects — one per agent declared in `reyn.yaml`:

```json
[
  {"name": "default", "role": "General-purpose assistant"},
  {"name": "researcher", "role": "Domain research and synthesis"}
]
```

#### `send_to_agent(agent_name, message)`

Submits one user-style message to a named agent and blocks (up to `--timeout` seconds) for the final reply.

Returns:

```json
{"reply": "...", "partial": false, "agent": "default"}
```

If `partial=true`, the timeout fired before the agent went idle — call again to receive more. Multi-turn continuity is preserved: each agent's `ChatSession` persists `history.jsonl` between calls.

### `serve` examples

```bash
# Start against the current directory
reyn mcp serve

# Explicit project path (required in most MCP client configs)
reyn mcp serve --project /path/to/your/project

# Extended timeout for long-running agent turns
reyn mcp serve --project /path/to/your/project --timeout 180
```

Wire into Claude Code's `mcp.json` (stdio transport):

```json
{
  "mcpServers": {
    "reyn": {
      "command": "/absolute/path/to/venv/bin/reyn",
      "args": [
        "mcp", "serve",
        "--project", "/absolute/path/to/your/reyn-project"
      ]
    }
  }
}
```

---

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Success / clean shutdown. |
| `1` | Configuration error — `reyn.yaml` not found, permission denied, schema version mismatch. |
| other | Unexpected exception. |

## See also

- [Concepts: MCP](../../concepts/mcp.md) — conceptual model, two roles, security model
- [Concepts: secret handling](../../concepts/secret-handling.md) — `~/.reyn/secrets.env` and `${VAR}` interpolation
- [Concepts: permission model](../../concepts/permission-model.md) — `mcp_install` permission
- [Reference: `reyn secret`](secret.md) — universal secret management
- [Reference: `reyn.yaml`](../config/reyn-yaml.md) — `mcp.servers:` schema and `permissions.mcp_install:`
- [Reference: common flags](common-flags.md) — flags shared across CLI commands
- [How-to: use an MCP server](../../guide/for-skill-authors/use-an-mcp-server.md)
