---
type: concept
topic: integration
audience: [human, agent]
---

# MCP (Model Context Protocol)

reyn speaks MCP in both directions: it can call out to external MCP servers (as a client), and it can expose its own agents to external LLM clients (as a server). The two roles are distinct and both are implemented.

## What is MCP

MCP is a JSON-RPC protocol for AI agents to connect to "servers" that expose tools. The spec is published by Anthropic at [modelcontextprotocol.io](https://modelcontextprotocol.io). Many official server implementations exist (`filesystem`, `git`, `github`, `fetch`, `brave-search`); third parties ship dozens more. A server advertises its tool list (`tools/list`) and executes calls (`tools/call`); the agent stays generic.

The point: your skill says "call the `read_text_file` tool on the `filesystem` server", not "shell out to `cat`". Swapping the backend is a config change, not a code change.

## Two roles Reyn plays

| Role | Direction | How |
|------|-----------|-----|
| **MCP client** — Reyn calls external servers | Outbound | The `mcp` Control IR op + `permissions.mcp:` declaration in a phase. A skill says "call this tool on this server"; the OS dispatches via `MCPClient` (stdio / http / sse). Example: a skill reads files through the `filesystem` MCP server. |
| **MCP server** — external clients call Reyn | Inbound | `reyn mcp serve --project .` launches Reyn as a JSON-RPC server. Claude Code, Cursor, OpenAI Agents SDK, or any MCP-aware client can then call INTO Reyn's agents using two tools: `list_agents()` and `send_to_agent(agent_name, message)`. |

The rest of this page covers each role in turn.

## Quick start: try MCP from `reyn chat` (no skill needed)

If you just want to use an MCP server interactively, you do **not** need to write a skill. `reyn chat` already exposes three router tools that work the same way Claude Code's `claude mcp` UX does:

| Tool | What it does |
|------|--------------|
| `list_mcp_servers` | Returns the names of all servers configured in `reyn.yaml` |
| `list_mcp_tools(server)` | Returns the tools exposed by one server |
| `call_mcp_tool(server, tool, args)` | Invokes a tool on a server, returning its result |

The LLM router can call these directly during a chat turn. Typical first-time flow:

```sh
# 1. Add a server entry to reyn.yaml (one-time)
mcp:
  servers:
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]

# 2. Pre-approve in reyn.yaml or accept the prompt on first use
permissions:
  mcp:
    filesystem: allow

# 3. Just chat
reyn chat
> このディレクトリにある README.md を要約して
```

The router invokes `list_mcp_tools` → `call_mcp_tool` automatically; no `permissions.mcp:` declaration in any skill is required. **Skill authoring is for when you want to formalize a recurring workflow** (= phase graph, validation, retry policy) — not a prerequisite to using MCP. The deep-dive below is for that case; if you only need ad-hoc invocation, you can stop reading here.

## Role 1: MCP client — Reyn calls external servers

When a skill needs an external tool, the flow is:

```
phase frontmatter         LLM emits Control IR        OS dispatches
  permissions:        →     {kind: mcp,           →   MCPClient
    mcp: [filesystem]        server: filesystem,        (stdio | http | sse)
                             tool: read_text_file,
                             args: {path: ...}}
```

1. The skill's phase declares `permissions.mcp: [server_name]` in frontmatter — without this, the runtime refuses every call to that server.
2. The LLM emits an `mcp` Control IR op: `{server, tool, args}`. It cannot invent server names; only servers configured in `reyn.yaml` and declared in the phase's permissions are reachable.
3. The OS resolves the server's transport (`stdio`, `http`, `sse`), dispatches via `MCPClient`, and returns the tool result to the phase loop.
4. Every call emits events — `mcp_called` before, `mcp_completed` (or `mcp_failed`) after. The audit trail is identical to any other op.

The boundary is sharp on purpose: skills describe what they want, the OS decides how to get it. Adding a new MCP server doesn't touch any OS code (P7).

## Transport choice (stdio vs HTTP)

Most official MCP servers are local processes you launch over stdio. A few hosted services expose HTTP endpoints. SSE transport is reserved for a future release.

| Transport | When | How reyn launches it |
|-----------|------|----------------------|
| `stdio`   | Local CLI server (most official servers — `filesystem`, `git`, `github`, `fetch`) | Spawns `command` with `args` and `env`; speaks JSON-RPC over stdin/stdout |
| `http`    | Hosted service (your own backend, an org-internal tool registry) | POSTs to `url` with `headers`; reuses one session per run |
| `sse`     | Streaming HTTP variant; rare | Same as `http` plus an event stream |

Pick `stdio` for anything you `npx` or `pip install` locally. Pick `http` when the server is operated by someone else and you've been handed a URL.

## Configuration

MCP servers are declared under `mcp.servers:` in `reyn.yaml`. Every entry has a `type`; the rest depends on the transport.

```yaml
# reyn.yaml
mcp:
  servers:
    # stdio: local process, speaks JSON-RPC over stdin/stdout
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
      env:
        # Optional. ${VAR} expands from os.environ at startup.
        FS_LOG_LEVEL: "info"

    # http: hosted server, JSON-RPC over Streamable HTTP
    internal_tools:
      type: http
      url: https://tools.example.internal/mcp
      headers:
        Authorization: "Bearer ${INTERNAL_TOOLS_TOKEN}"
```

| Field | stdio | http | Description |
|-------|-------|------|-------------|
| `type` | required | required | `stdio` \| `http` \| `sse` |
| `command` | required | — | Executable to spawn (e.g., `npx`, `python`, an absolute path) |
| `args`    | optional | — | Argument list passed to `command` |
| `env`     | optional | — | Extra environment variables for the spawned process |
| `url`     | — | required | Endpoint URL |
| `headers` | — | optional | Static headers; values support `${VAR}` expansion |

`${VAR}` expansion happens at op-dispatch time using `os.environ`. Missing variables expand to `""` and emit a warning — never a hard error, so a missing optional token doesn't crash the run.

API keys belong in environment variables, never inline in `reyn.yaml` (see [reyn.yaml: API keys](../reference/config/reyn-yaml.md#api-keys)).

## Security model

MCP calls cross two gates before they leave the process:

1. **Phase declaration.** A phase MUST list each server it intends to use under `permissions.mcp` in its frontmatter. The runtime calls `require_mcp(decl, server, ...)`; if `server not in decl.mcp`, the call fails with a clear error pointing at the missing declaration.
2. **Approval.** Like every other capability, the first invocation per skill prompts (`y` / `j` / `r` / `N`). Persistent approvals land in `.reyn/approvals.yaml` keyed by `<skill>/mcp.<server>`. Pre-approve project-wide with `permissions.mcp: allow` in `reyn.yaml` if you trust the project broadly.

This matches reyn's general permission model — see [permission-model.md](permission-model.md). One skill's MCP approval doesn't leak to another skill, and a sub-skill invoked via `run_skill` has to ask for its own permissions.

Three audit events are emitted per call:

| Event | When | Payload |
|-------|------|---------|
| `mcp_called` | Before the request leaves the process | `server`, `tool`, `args` |
| `mcp_completed` | On normal return | `server`, `tool`, `is_error` |
| `mcp_failed` | On transport / protocol error | `server`, `tool`, `error` |

Filter for them with `reyn events tail | grep mcp_` or `grep '"mcp_called"' .reyn/events.jsonl`.

## The first stdlib skill: `read_local_files`

`read_local_files` is the canonical example of a skill built around an MCP server. It pairs with the `filesystem` MCP server to read files inside a project, summarise sections, and answer questions about the contents — everything a vanilla `cat` would do, except routed through the permission system and the audit log.

Treat it as the template to copy when authoring your own MCP-backed skill: declare `permissions.mcp: [filesystem]` in the phase, emit `mcp` ops with `tool: read_text_file` (or whatever the server advertises), and let the OS handle the rest.

See the [reference page](../reference/stdlib/read_local_files.md) for phase shapes and the [how-to](../guide/for-skill-authors/use-an-mcp-server.md) for a full quickstart.

## Role 2: MCP server — external clients call Reyn

When you run `reyn mcp serve`, Reyn becomes an MCP server. External MCP-aware clients — Claude Code, Cursor, OpenAI Agents SDK, or anything that speaks the MCP protocol — can then submit messages to your Reyn agents as if they were just another MCP tool.

### Starting the server

```sh
reyn mcp serve --project /path/to/your/project
```

`--project` points at the directory containing `reyn.yaml`. Because MCP clients typically spawn the server process with `cwd=/`, this flag is required in most client configs — the server has no other way to locate your project. `--timeout` (default 60 s) controls how long `send_to_agent` blocks before returning a partial reply; the agent keeps working in the background.

The server speaks JSON-RPC over stdio. There is no port. The MCP client launches the process itself and owns the transport.

### Tools exposed

Two tools are registered:

| Tool | Signature | What it does |
|------|-----------|--------------|
| `list_agents` | `()` | Returns a JSON array of `{name, role}` objects — one entry per agent declared in `reyn.yaml`. |
| `send_to_agent` | `(agent_name, message)` | Submits one user-style message to the named agent and blocks (up to `--timeout` seconds) for the final reply text. Returns `{reply, partial, agent}`. If `partial=true`, the agent is still working; call again to receive more. |

Multi-turn continuity is preserved: each agent's `ChatSession` keeps its `history.jsonl` between calls, so a conversation that starts in Claude Code can be resumed from `reyn chat` — or vice versa.

### What "via MCP" means for your skills

External clients see agents, not the skill graph. From the outside, there are only two operations: list agents and send a message. The OS contract still applies on Reyn's side: permissions are checked, events are emitted, and all the normal validation runs. Skills can be approved non-interactively if `permissions: allow` is set in `reyn.yaml` (the MCP server runs without a human at stdin, so interactive prompts would block indefinitely).

This is part of Reyn's "talks-out + talked-to" multi-agent surface. See [multi-agent.md](multi-agent.md) for how agents relate to each other within a single Reyn process.

## What MCP is NOT for

MCP is the right tool for *external capability access*. Don't reach for it when:

- **You need heavy compute.** Use a Python preprocessor (`python` op). MCP calls cross a process boundary on every invocation; an inline NumPy step is much faster.
- **You're encoding a reusable workflow.** That's a skill, not an MCP server. Use `skill_builder` to author a new skill, not a new MCP tool.
- **You want cross-agent messaging.** Use `messages_to_agents` and topology rules. MCP doesn't model agent identity or chains.
- **You need state across invocations.** MCP servers can be stateless or stateful, but reyn treats each call as independent. Persistent state belongs in the workspace.

If you find yourself wishing MCP could do one of these, you're at the wrong layer.

## See also

- [How-to: use an MCP server](../guide/for-skill-authors/use-an-mcp-server.md) — quickstart with the filesystem server
- [Reference: `read_local_files`](../reference/stdlib/read_local_files.md) — the first stdlib MCP skill
- [Reference: `reyn.yaml`](../reference/config/reyn-yaml.md#mcp-servers) — full `mcp.servers:` schema
- [Concepts: permission model](permission-model.md) — where `permissions.mcp` fits
- [modelcontextprotocol.io](https://modelcontextprotocol.io) — the spec, server registry, official SDKs
