---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn mcp serve]
---

# `reyn mcp`

Expose Reyn agents to external MCP-aware clients over a JSON-RPC stdio transport.

## Synopsis

```
reyn mcp serve [--project PATH] [--timeout SECONDS] [common flags]
```

## Description

`reyn mcp serve` launches Reyn as an MCP (Model Context Protocol) JSON-RPC server. External MCP-aware clients — Claude Code, Cursor, OpenAI Agents SDK with MCP enabled, and any other client that speaks the MCP protocol — can then submit messages to your Reyn agents using two tools: `list_agents` and `send_to_agent`.

This is the inverse of Reyn's MCP-client role (where Reyn calls out to third-party MCP servers). Here, the external client calls INTO Reyn. The same `reyn.yaml` and agent registry that `reyn chat` uses backs the MCP server — permissions are checked, events are emitted, and all normal OS validation runs.

For the conceptual model and the Two roles framing, see [concepts/mcp.md — Role 2](../../concepts/mcp.md#role-2-mcp-server-external-clients-call-reyn).

## Subcommand: `serve`

The only subcommand available today.

`reyn mcp serve` starts a JSON-RPC server that speaks over stdio. There is no port; the MCP client launches the process itself and owns the transport. Because MCP clients (Claude Desktop, Cursor, Claude Code) typically spawn the server process with `cwd=/`, always pass `--project` in the client config's `args` list — the server has no other way to locate your `reyn.yaml`.

On startup the server:

1. Reads `reyn.yaml` from the project root and loads the agent registry.
2. Replays the WAL into per-agent snapshots so any stranded in-flight skills resume cleanly (same behavior as `reyn chat` startup).
3. Enters the MCP JSON-RPC loop and waits for tool calls.

On EOF from stdin (= the MCP client disconnects), the registry is shut down cleanly and all in-flight sessions drain.

The server runs non-interactively. No human is at the stdin that the MCP transport owns, so interactive permission prompts would block indefinitely. Pre-approve skill permissions in `reyn.yaml` with `permissions: allow` before wiring up the server.

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--project PATH` | Closest ancestor directory containing `reyn.yaml`, else fails with exit code 1 | Project root. Required in most client configs because MCP clients ignore the `cwd` field when spawning the server process. |
| `--timeout SECONDS` | `60.0` | Maximum blocking time per `send_to_agent` call. On timeout the call returns whatever reply has accumulated; the agent keeps working in the background. The next `send_to_agent` call will receive the rest. |

Inherited common flags (`--model`, `--output-language`, `--max-phase-visits`, etc.) are accepted; see [common-flags.md](common-flags.md).

## Tools exposed

Two MCP tools are registered under the server name `reyn`:

### `list_agents()`

Returns a JSON array of objects — one per agent declared in `reyn.yaml`:

```json
[
  {"name": "default", "role": "General-purpose assistant"},
  {"name": "researcher", "role": "Domain research and synthesis"}
]
```

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Agent name, as declared in `reyn.yaml` or created via `reyn agent new`. |
| `role` | string | First line of the agent's role description from `profile.yaml`. Empty string if the profile has no role. |

### `send_to_agent(agent_name, message)`

Submits one user-style message to a named agent and blocks (up to `--timeout` seconds) for the final reply.

| Parameter | Type | Description |
|-----------|------|-------------|
| `agent_name` | string | Name of the agent. Use `list_agents` to enumerate available agents. |
| `message` | string | User message body. |

Returns a JSON object:

```json
{"reply": "...", "partial": false, "agent": "default"}
```

| Field | Type | Description |
|-------|------|-------------|
| `reply` | string | Agent's reply text. If `partial=true` and no reply was emitted before timeout, contains a descriptive placeholder. |
| `partial` | boolean | `true` if the timeout fired before the agent went idle. The agent's task continues in the background; call again to receive more. |
| `agent` | string | Echo of the `agent_name` parameter. |

Multi-turn continuity is preserved across calls: each agent's `ChatSession` persists `history.jsonl` between calls. A conversation started via `reyn mcp serve` can be resumed from `reyn chat`, and vice versa.

## Examples

Start the MCP server against the current directory's project:

```bash
reyn mcp serve
```

Start with an explicit project path (required in most MCP client configs):

```bash
reyn mcp serve --project /path/to/your/project
```

Allow more time for long-running agent turns:

```bash
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

## Exit codes

| Code | Meaning |
|------|---------|
| `0` | Clean shutdown — EOF on stdin (MCP client disconnected). |
| `1` | Configuration error — `reyn.yaml` not found, or schema version mismatch in the WAL. |
| other | Unexpected exception. |

## See also

- [Concepts: MCP — Two roles](../../concepts/mcp.md) — conceptual model
- [Reference: reyn chat](chat.md) — interactive REPL alternative
- [Reference: reyn.yaml](../config/reyn-yaml.md) — agent config and MCP server declarations
- [Reference: common flags](common-flags.md) — flags shared across CLI commands
- [Reference: permissions](../config/permissions.md) — pre-approving skills for non-interactive use
