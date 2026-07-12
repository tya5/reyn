---
type: concept
topic: integration
audience: [human, agent]
---

# MCP (Model Context Protocol)

reyn speaks MCP in both directions: it can call out to external MCP servers (as a client), and it can expose its own agents to external LLM clients (as a server). The two roles are distinct and both are implemented.

## What is MCP

MCP is a JSON-RPC protocol for AI agents to connect to "servers" that expose tools. The spec is published by Anthropic at [modelcontextprotocol.io](https://modelcontextprotocol.io). Many official server implementations exist (`filesystem`, `git`, `github`, `fetch`, `brave-search`); third parties ship dozens more. A server advertises its tool list (`tools/list`) and executes calls (`tools/call`); the agent stays generic.

The point: your workflow says "call the `read_text_file` tool on the `filesystem` server", not "shell out to `cat`". Swapping the backend is a config change, not a code change.

## Two roles Reyn plays

| Role | Direction | How |
|------|-----------|-----|
| **MCP client** — Reyn calls external servers | Outbound | The `mcp` Control IR op + `permissions.mcp:` declaration in a phase. A workflow says "call this tool on this server"; the OS dispatches via `MCPClient` (stdio / http / sse). Example: a workflow reads files through the `filesystem` MCP server. |
| **MCP server** — external clients call Reyn | Inbound | `reyn mcp serve --project .` launches Reyn as a JSON-RPC server. Claude Code, Cursor, OpenAI Agents SDK, or any MCP-aware client can then call INTO Reyn's agents using two tools: `list_agents()` and `send_to_agent(agent_name, message)`. |

The rest of this page covers each role in turn.

## Quick start: from zero to working MCP in three commands

The recommended first-time flow uses `reyn mcp install` — no manual YAML editing required:

```bash
# 1. Discover available servers
reyn mcp search "github"

# 2. Install (handles config + credentials + permission gate)
reyn mcp install io.github.modelcontextprotocol/server-github

# 3. Start using it immediately
reyn chat
> このリポジトリの最近の PR を一覧して
```

`reyn mcp install` fetches the server manifest from the MCP registry, checks that the required runtime (`npx`, `uvx`, etc.) is installed, prompts for any credentials (storing them securely in `~/.reyn/secrets.env`), and writes the `mcp.servers.*` entry into your config with `${VAR}` references for secrets — all in one step.

**For servers not in the registry** (including Anthropic's official reference servers like `@modelcontextprotocol/server-filesystem`), use `--source`:

```bash
reyn mcp install --source npm:@modelcontextprotocol/server-filesystem
reyn mcp install --source pypi:mcp-server-fetch
reyn mcp install --source docker:mcp/playwright
reyn mcp install --source https://github.com/modelcontextprotocol/servers/tree/main/src/filesystem
```

`--source` skips the registry fetch and resolves install metadata from the source specifier directly. Permission gate, credentials, config write, and audit event are identical to the registry path.

For the full `reyn mcp` CLI reference, see [Reference: `reyn mcp`](../../reference/cli/mcp.md).

## Quick start: try MCP from `reyn chat` (manual config path)

If you prefer to configure a server manually (or are adding a server not in the public registry), add it directly to `reyn.yaml`. `reyn chat` exposes verb actions under the `mcp` category:

| Action | What it does |
|------|--------------|
| `mcp__search_registry({text})`                              | Search the official MCP registry for matching servers |
| `mcp__install_registry({server_id})`                        | Install a server from the official MCP registry |
| `mcp__install_package({kind, identifier, version?})`        | Install via a third-party package channel (npm / pypi / docker / GitHub URL) |
| `mcp__install_local({name, command, args})`                 | Register a local command (e.g. LLM-authored script) as an MCP server |
| `mcp__list_servers()`                                       | Returns the names of all servers configured in `.reyn/mcp.yaml` |
| `mcp__list_tools({server})`                                 | Returns the tools exposed by one server (each entry has `name="<server>__<tool>"`, `description`, `inputSchema`) |
| `mcp__call_tool({tool, tool_args})`                              | Call a tool by `<server>__<tool>` identifier (from `mcp__list_tools`) with its declared tool_args |
| `mcp__drop_server({server})`                                | Remove an installed server from the config |

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

The router invokes `mcp__list_tools` → `mcp__call_tool` automatically; no `permissions.mcp:` declaration in any workflow is required. **Workflow authoring is for when you want to formalize a recurring workflow** (= validation, retry policy) — not a prerequisite to using MCP. The deep-dive below is for that case; if you only need ad-hoc invocation, you can stop reading here.

## Role 1: MCP client — Reyn calls external servers

When a workflow needs an external tool, the flow is:

```
phase frontmatter         LLM emits Control IR        OS dispatches
  permissions:        →     {kind: mcp,           →   MCPClient
    mcp: [filesystem]        server: filesystem,        (stdio | http | sse)
                             tool: read_text_file,
                             args: {path: ...}}
```

1. The workflow's phase declares `permissions.mcp: [server_name]` in frontmatter — without this, the runtime refuses every call to that server.
2. The LLM emits an `mcp` Control IR op: `{server, tool, args}`. It cannot invent server names; only servers configured in `reyn.yaml` and declared in the phase's permissions are reachable.
3. The OS resolves the server's transport (`stdio`, `http`, `sse`), dispatches via `MCPClient`, and returns the tool result to the phase loop.
4. Every call emits events — `mcp_called` before, `mcp_completed` (or `mcp_failed`) after. The audit trail is identical to any other op.

The boundary is sharp on purpose: workflows describe what they want, the OS decides how to get it. Adding a new MCP server doesn't touch any OS code (P7).

### Resources: list + read

Alongside tools, a server can expose **resources** (server-hosted content addressed by URI — files, database rows, generated documents) and **resource templates** (parameterized URI patterns the LLM fills in). Reyn's chat surface mirrors the tools flow:

- `list_mcp_resources(server)` / `list_mcp_resource_templates(server)` — discovery, unpermissioned (mirrors `list_mcp_tools`; no Control IR op kind, no `permissions.mcp` gate — resource *metadata* carries no more risk than a tool's name/description).
- `read_mcp_resource(server, uri)` — reads one resource's contents. This one IS gated: it's a `mcp_read_resource` Control IR op, requiring the same `permissions.mcp: [server_name]` grant as a tool call, because a resource's *contents* are external, potentially sensitive server-authored data — the same reasoning `call_mcp_tool` already applies to a tool result. Every read emits `mcp_resource_read` before, `mcp_resource_read_completed` (or `_failed`) after.

Both list and read are additionally gated by the server's **negotiated capabilities**: a server that never advertised `resources` in its `initialize` handshake fails fast with a clear error (`require_capability` in `reyn/mcp/client.py`) instead of a raw protocol error.

### Resource subscriptions: the async push event-source

`resources/subscribe` is a **state-sync/watch** mechanism, not a message queue: subscribe to a URI, and the server pushes a thin `notifications/resources/updated {uri}` signal (no content) whenever that resource changes — the client re-reads (`read_mcp_resource`) to see what changed.

- `subscribe_mcp_resource(server, uri)` / `unsubscribe_mcp_resource(server, uri)` — both gated the same way `read_mcp_resource` is (`mcp_subscribe_resource` / `mcp_unsubscribe_resource` Control IR ops, `permissions.mcp: [server_name]`), plus an ADDITIONAL gate on the server's negotiated `resources.subscribe` sub-capability — a server can advertise `resources` (list/read work) without advertising `subscribe` (e.g. every server built with FastMCP's high-level `FastMCP()` class today — the underlying SDK hard-codes `resources.subscribe=False` regardless of what handlers a FastMCP server registers).
- **Persistent connection required.** A subscription only makes sense on a held (session-lifetime) connection — the subscribed-URI set lives in-memory on `MCPConnectionService` (runtime-only; a subscription carries no data of its own, so it's fully re-establishable, never WAL'd). An ephemeral chat session refuses both ops with a clear error rather than accept a subscription that dies the instant its one-shot connection closes.
- **Survives a transport-death reconnect.** A dropped connection (subprocess death, broken HTTP) re-opens a fresh MCP session with no memory of prior subscriptions; `MCPConnectionService` automatically re-issues every tracked subscription against the fresh connection, so a subscription set up before a drop keeps delivering pushes after reyn heals the connection.
- **The push lands on the EventLog, not in a tool result.** `mcp_resource_updated` (`server`, `uri`, `resync`) is emitted asynchronously whenever the notification arrives — independent of any Control IR op call. It is also wired into the hook dispatcher as an **external-event hook-point**, so a hook can react to it directly — see [Hooks § External-event points](../runtime/hooks.md#external-event-points) — while the EventLog remains an audit-trail signal a workflow author can read back independent of any hook.

### Prompts: list + get

Alongside tools and resources, a server can expose **prompts** (named, server-authored prompt templates the LLM can render with arguments). Reyn's chat surface mirrors the resources flow exactly:

- `list_mcp_prompts(server)` — discovery, unpermissioned (mirrors `list_mcp_resources`/`list_mcp_tools`; no Control IR op kind, no `permissions.mcp` gate). Returns each prompt's `name` + `description` + `arguments` schema.
- `get_mcp_prompt(server, name, arguments?)` — fetches one rendered prompt's messages. This one IS gated: it's a `mcp_get_prompt` Control IR op, requiring the same `permissions.mcp: [server_name]` grant as a tool call / resource read, because a rendered prompt's *messages* are external, potentially sensitive server-authored content. Every get emits `mcp_prompt_get` before, `mcp_prompt_get_completed` (or `_failed`) after.

Both list and get are additionally gated by the server's **negotiated capabilities**: a server that never advertised `prompts` in its `initialize` handshake fails fast with a clear error (`require_capability` in `reyn/mcp/client.py`) instead of a raw protocol error.

Prompts have no subscribe concept — MCP defines no per-prompt push notification (only the coarser `notifications/prompts/list_changed`, already bridged to an `mcp_prompt_list_changed` EventLog event); there is no `subscribe_mcp_prompt`.

### Elicitation: structured input requests from a server

A server can ask the user a flat/primitive-schema question through reyn's own consent path — the server issues an `elicitation/create` request, and reyn turns it into a user intervention prompt.

- **Server attribution.** The prompt always names the asking server and states this is not reyn (e.g. `⚠️ MCP server 'github' asks (this is NOT reyn): ...`); individual field prompts carry the same `[MCP server '<name>']` prefix.
- **One prompt for a single closed-set field.** A question that resolves to a single yes/no or enum choice (e.g. a `confirm`) is shown as ONE prompt — the attributed banner, whose choices are the answer plus an explicit `decline`. It is not split into a separate accept/decline gate followed by a redundant value prompt. Multi-field questions still show the accept/decline gate first, then one prompt per field; a single free-text field also keeps its gate.
- **Sensitive-field warning.** A field whose name or description contains `password`, `token`, `key`, `secret`, or `credential` gets an extra confirmation step first, stating explicitly that the answer will be sent to the server and that reyn never autofills it from env vars or stored secrets.
- **No autofill.** Every answer is human-typed — this path never reads env vars or the secrets store to prefill a field.
- **Configuration** (per server, under `mcp.servers.<name>`): `elicitation` (`prompt` (default) | `auto_decline`), `elicitation_timeout_seconds` (default 120).
- **Semantics**: a timeout returns `cancel`; an explicit human decline, an `auto_decline`-configured server, or a headless context (no live listener) all return `decline`.
- **Audit records field *keys* only, never values.** The `mcp_elicitation_requested`/`_answered`/`_timed_out`/`_auto_declined` events carry only the requested schema's property names (e.g. `field_keys: ["reason", "priority"]`) — never the human's typed answer.

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

`${VAR}` expansion resolves from `os.environ` (which is pre-loaded from `~/.reyn/secrets.env` at startup — see [Concepts: secret handling](../runtime/secret-handling.md)). Missing variables expand to `""` and emit a warning — never a hard error, so a missing optional token doesn't crash the run.

The `${VAR}` syntax works in **all** YAML string fields, not just `mcp.servers`. This means `models.<name>.api_key`, `litellm.api_base`, and future fields all use the same mechanism. See [Reference: `reyn.yaml` — `${VAR}` interpolation](../../reference/config/reyn-yaml.md#var-interpolation) for the full picture.

API keys and tokens belong in `~/.reyn/secrets.env` (managed via `reyn secret set`), referenced as `${VAR}` in `reyn.yaml` — never as literal values inline. See [Concepts: secret handling](../runtime/secret-handling.md).

### OAuth

For a server that requires OAuth 2.1 rather than a static bearer token, add
`auth` to its `http`-transport entry — OAuth is **only supported over
Streamable HTTP**; `stdio`/`sse` servers reject an `auth` key outright.

```yaml
mcp:
  servers:
    hosted_tool:
      type: http
      url: https://tools.example.com/mcp
      auth: oauth   # shorthand for {type: oauth}
      # or the long form, when you need scopes / a specific client:
      # auth:
      #   type: oauth
      #   scopes: [read, write]
      #   client_id: ${HOSTED_TOOL_CLIENT_ID}
      #   client_secret: ${HOSTED_TOOL_CLIENT_SECRET}
```

- **First auth is interactive**: reyn opens a browser and a localhost
  callback server to complete the authorization code flow. A headless run
  (no interactive session, no cached token yet) fails with a clear error
  instead of hanging on a browser round-trip nobody can complete — run reyn
  interactively once against the server first.
- **Tokens are cached** in `~/.reyn/oauth_tokens.json` (mode `0600`,
  per-server) — the same store reyn's device-grant OAuth already uses, in
  the "outside" bucket of the [`.reyn/` layout](../../reference/runtime/reyn-dir-layout.md):
  operator/user-owned, never written through a WAL-emitting op, never
  captured by rewind/PITR. Once cached, subsequent runs — including
  headless ones — reuse the token without a browser round-trip.
- **Static bearer auth is unaffected**: a server that just needs
  `headers: {Authorization: "Bearer ${TOKEN}"}` keeps working exactly as
  before — `auth` is only for the OAuth 2.1 flow.

## Security model

MCP operations are gated at two points:

### Install-time gate: `file.write` + `http.get`

Before any MCP server can be added to the configuration, the install op's writes go through the OS's standard list-axis gates. The legacy `permissions.mcp_install: ask | allow | deny` bool axis was removed in the collapse arc — install gating now flows through:

- `file.write` on `.reyn/mcp.yaml` (= the canonical mutation target). `startup_guard` prompts the operator once per workflow+path; runtime is silent after approval.
- `http.get` on `registry.modelcontextprotocol.io` (= the registry fetch). Same prompt model.
- `secret.write` on the env-var keys the registry declares as `isSecret` (= wildcard `"*"` because the key set is runtime-determined).

Enterprise teams point reyn at private / corporate registries via either of two equivalent mechanisms:

**A. `reyn.yaml mcp.registries:` list config** — declarative, project-scoped, version-controlled:

```yaml
# reyn.yaml (project scope — committed to git)
mcp:
  registries:
    - https://mcp-registry.internal.acme.com   # private registry (tried first)
    - https://registry.modelcontextprotocol.io  # public fallback
permissions:
  web.fetch: allow      # blanket allow for registry fetches
  file.write: allow     # blanket approval for .reyn/mcp.yaml writes
```

**B. `REYN_MCP_REGISTRY_URLS` (plural) env var** — explicit operator override, useful for CI / per-shell config:

```bash
# operator's shell rc / systemd unit / CI runner env
export REYN_MCP_REGISTRY_URLS="https://mcp-registry.internal.acme.com,https://registry.modelcontextprotocol.io"
```

The env var wins when both are set (= explicit operator override beats declarative config). The legacy singular `REYN_MCP_REGISTRY_URL` is honored as a one-item list for backward compat.

Both the async op-handler client (`reyn.core.registry.client`) and the safe-mode skill-internal lookup (`reyn.api.safe.mcp.registry`) iterate the list in order with the following fallback semantics:

| Operation | Behavior |
|---|---|
| `lookup(server_id)` | Try each URL in order; first non-404 hit wins; all 404 → `None`; non-404 error after 404 re-raises |
| `search(query)` | Try each URL in order; first non-empty result wins; all empty → `[]` |

This implements the "private first, public fallback" pattern: servers from the private registry shadow same-named public entries, and the public registry serves as a discoverability fallback for unrelated names.

See [Concepts: permission model](../runtime/permission-model.md) → "Collapse arc" for the full migration story.

> Legacy `permissions.mcp_install: ...` keys in older `reyn.yaml` files are accepted with a `DeprecationWarning` and translate to the equivalent gates during the migration window.

### Runtime gate: `permissions.mcp`

MCP tool calls cross two checks before they leave the process:

1. **Phase declaration.** A phase MUST list each server it intends to use under `permissions.mcp` in its frontmatter. The runtime calls `require_mcp(decl, server, ...)`; if `server not in decl.mcp`, the call fails with a clear error pointing at the missing declaration.
2. **Approval.** Like every other capability, the first invocation per workflow prompts (`y` / `j` / `r` / `N`). Persistent approvals land in `.reyn/approvals.yaml` keyed by `<skill>/mcp.<server>`. Pre-approve project-wide with `permissions.mcp: allow` in `reyn.yaml` if you trust the project broadly.

This matches reyn's general permission model — see [../runtime/permission-model.md](../runtime/permission-model.md). One skill's MCP approval doesn't leak to another skill, and a sub-skill invoked via `run_skill` has to ask for its own permissions.

Three audit events are emitted per call:

| Event | When | Payload |
|-------|------|---------|
| `mcp_called` | Before the request leaves the process | `server`, `tool`, `args` |
| `mcp_completed` | On normal return | `server`, `tool`, `is_error` |
| `mcp_failed` | On transport / protocol error | `server`, `tool`, `error` |

Filter for them with `reyn events tail | grep mcp_` or `grep '"mcp_called"' .reyn/events.jsonl`.

## Skills that use MCP

A skill declares `permissions.mcp: [<server>]` in the phase, emits `mcp` ops with `tool: <name>` (or whatever the server advertises), and lets the OS handle the rest. See the how-to for a full quickstart on authoring your own MCP-backed skill.

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

Multi-turn continuity is preserved: each agent's `Session` keeps its `history.jsonl` between calls, so a conversation that starts in Claude Code can be resumed from `reyn chat` — or vice versa.

### What "via MCP" means for your workflows

External clients see agents, not the workflow graph. From the outside, there are only two operations: list agents and send a message. The OS contract still applies on Reyn's side: permissions are checked, events are emitted, and all the normal validation runs. Workflows can be approved non-interactively if `permissions: allow` is set in `reyn.yaml` (the MCP server runs without a human at stdin, so interactive prompts would block indefinitely).

This is part of Reyn's "talks-out + talked-to" multi-agent surface. See [../multi-agent/multi-agent.md](../multi-agent/multi-agent.md) for how agents relate to each other within a single Reyn process.

## What MCP is NOT for

MCP is the right tool for *external capability access*. Don't reach for it when:

- **You need heavy compute.** Use a Python preprocessor (`python` op). MCP calls cross a process boundary on every invocation; an inline NumPy step is much faster.
- **You're encoding a reusable workflow.** That's a skill, not an MCP server. Use `skill_builder` to author a new skill, not a new MCP tool.
- **You want cross-agent messaging.** Use `messages_to_agents` and topology rules. MCP doesn't model agent identity or chains.
- **You need state across invocations.** MCP servers can be stateless or stateful, but reyn treats each call as independent. Persistent state belongs in the workspace.

If you find yourself wishing MCP could do one of these, you're at the wrong layer.

## See also


- [Reference: `reyn mcp`](../../reference/cli/mcp.md) — full CLI reference for `search`, `install`, `list`, `remove`, `set-secret`, `clear-secret`
- [Reference: `reyn secret`](../../reference/cli/secret.md) — universal secret management
- [Concepts: secret handling](../runtime/secret-handling.md) — `~/.reyn/secrets.env` and `${VAR}` interpolation
- [Reference: `reyn.yaml`](../../reference/config/reyn-yaml.md#mcp-servers) — full `mcp.servers:` schema
- [Concepts: permission model](../runtime/permission-model.md) — `file.write` / `http.get` / `permissions.mcp` and the collapse arc
- [Concepts: hooks](../runtime/hooks.md#external-event-points) — the `mcp_resource_updated` external-event hook-point
- [modelcontextprotocol.io](https://modelcontextprotocol.io) — the spec, server registry, official SDKs
