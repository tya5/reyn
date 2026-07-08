---
type: reference
topic: runtime
audience: [human, agent]
---

# Control IR

Control IR is the list of side-effect operations the LLM may emit alongside its artifact. The OS dispatches each op and returns the result for the LLM (or the next phase) to consume.

## Op kinds

| Kind | Purpose | Permission required |
|------|---------|---------------------|
| `read_file` | Read a file (optionally a line range) | `file.read` |
| `write_file` | Write (create / overwrite) a file | `file.write` |
| `edit_file` | Replace a string in a file | `file.write` |
| `delete_file` | Delete a file | `file.write` |
| `glob_files` | List files matching a glob pattern | `file.read` |
| `grep_files` | Search file contents by regex | `file.read` |
| `ask_user` | Pause the phase and ask the user a question | none (always allowed) |
| `present` | Route bulk data + a display template to the user surface without the data passing through LLM output tokens (fire-and-continue) | Tier 0 (always allowed); `data_ref` read authority == `file.read` |
| `sandboxed_exec` | Run argv under a `SandboxPolicy` via a `SandboxBackend` (replaces the removed `shell` op) | enforced by backend (`SandboxPolicy`) |
| `web_search` | Search the public web via DuckDuckGo | Tier 1 — default allow; `web.search: deny` in `reyn.yaml` blocks |
| `web_fetch` | Fetch a single URL and return extracted text | Tier 1 — default allow; `web.fetch: deny` in `reyn.yaml` blocks |
| `mcp` | Call a tool on a configured MCP server | `permissions.mcp: [server_name]` in skill frontmatter |
| `mcp_read_resource` | Read one resource (or a resolved resource-template URI) on a configured MCP server | `permissions.mcp: [server_name]` in skill frontmatter (same axis as `mcp`) |
| `mcp_subscribe_resource` | Subscribe to server-pushed `resources/updated` notifications for one resource URI (requires a persistent connection — see below) | `permissions.mcp: [server_name]` in skill frontmatter (same axis as `mcp`) |
| `mcp_unsubscribe_resource` | Cancel a previous `mcp_subscribe_resource` | `permissions.mcp: [server_name]` in skill frontmatter (same axis as `mcp`) |
| `mcp_get_prompt` | Fetch one rendered prompt (messages) by name from a configured MCP server | `permissions.mcp: [server_name]` in skill frontmatter (same axis as `mcp`) |
| `mcp_install` | Install an MCP server from the registry into the project config | `permissions.mcp_install: true` in skill frontmatter |
| `mcp_drop_server` | Remove an MCP server from project/local/user config (inverse of `mcp_install`) | `permissions.mcp_drop_server: true` in skill frontmatter |
| `skill_install` | Register a skill (local dir or git/URL source) into the project skills config | `file.write: [.reyn/config/skills.yaml]` in skill frontmatter; `http.get: [{host: <source_host>}]` when `source` is set |
| `pipeline_install` | Register a pipeline (local DSL file or git/URL source) into the project pipelines config | `file.write: [.reyn/config/pipelines.yaml]` in skill frontmatter; `http.get: [{host: <source_host>}]` when `source` is set |
| `index_query` | Semantic vector search over one indexed source | none |
| `recall` | Macro: embed query (provider-direct) → index_query per source → merge top-K | none (embedding API cost) |
| `index_drop` | Remove an indexed source entirely (destructive) | `permissions.index_drop: ask` in skill frontmatter |
| `judge_output` | LLM scorer: rubric + threshold + `on_fail` policy | none (LLM cost) |
| `compact` | Voluntarily compact the conversation/phase history (advisory) | none (LLM cost; the mandatory `retry_loop` backstop is independent) |
| `task.create` | Create a Task (`deps` for ordering; `link_type` `awaited`/`background` sets whether a sub-task gates the parent's completion — §2187; sub-task ownership is OS-derived from execution context — §16) | requester-gated (caller becomes requester) |
| `task.update_status` | Declare a status transition | assignee-gated (single-writer CAS on `assignee == caller session_id`) |
| `task.get` | Read one Task record | requester-gated |
| `task.list` | List Tasks (filter by assignee / requester / status); `requester=<task-id>` lists sub-tasks owned by that task | none (filtered read) |
| `task.add_dependency` | Add a depends-on edge (dependency DAG) | requester-gated |
| `task.remove_dependency` | Drop a depends-on edge (idempotent); may promote a now-satisfied dependent | requester-gated |
| `task.repoint_dependency` | Atomically repoint an edge `from_depends_on`→`to_depends_on` (cycle-checked first); re-evals readiness | requester-gated |
| `task.abort` | Remove-op (= delete): archive the task + sub-tree (cooperative-terminal) | requester-gated |
| `task.heartbeat` | Liveness / unblock-predicate trigger for a blocked Task | assignee-gated |
| `task.register_unblock_predicate` | Register a deterministic unblock predicate | assignee-gated |
| `task.comment` | Append a comment to a Task's thread | none |
| `task.assign` | Assign a session to a Task (§27-31 pending-assignment queue): claim an UNASSIGNED task or reassign an assigned one; rebinds the WAL subscription + OS-derives the now-startable status + wakes the new assignee | UNASSIGNED → any session may claim; assigned → current-assignee-gated (owner hand-off) |

## Common envelope

Every op is a JSON object with a `kind` discriminator:

```json
{
  "kind": "read_file",
  "path": "src/foo.py"
}
```

The OS validates the op against its kind's schema, executes it, and returns a result to the calling phase.

## File ops (fine-grained)

The LLM-emittable file operations are six fine-grained kinds — the same subset
the chat router exposes as tools (see
[concepts/architecture/llm-invocation-surfaces.md](../../concepts/architecture/llm-invocation-surfaces.md)).
Each is a distinct op kind with its own schema; there is no `op` sub-field.

```json
{"kind": "read_file", "path": "src/foo.py"}
{"kind": "read_file", "path": "src/foo.py", "offset": 100, "limit": 40}

{"kind": "write_file", "path": "out.txt", "content": "..."}

{"kind": "edit_file", "path": "src/foo.py",
 "old_string": "...", "new_string": "...", "replace_all": false}

{"kind": "delete_file", "path": "tmp.txt"}

{"kind": "glob_files", "path": ".", "pattern": "**/*.py", "max_results": 50}

{"kind": "grep_files", "path": "src", "pattern": "def \\w+",
 "glob": "**/*.py", "case_sensitive": false, "max_results": 50}
```

| Kind | Permission | Notes |
|------|-----------|-------|
| `read_file` | `file.read` | `offset` / `limit` (line range) optional. |
| `write_file` | `file.write` | Creates or overwrites; parent dirs created as needed. |
| `edit_file` | `file.write` | `old_string` must be unique unless `replace_all: true`. |
| `delete_file` | `file.write` | |
| `glob_files` | `file.read` | `path` defaults to `.`. |
| `grep_files` | `file.read` | `glob` filters which files are searched. |

Permission scopes are configured per-op kind. See `reference/config/permissions.md`.

A successful `edit_file` result additionally carries a `preview` (str): a
numbered-line view (`<lineno>\t<text>`, 1-based) of the changed region — the
lines around where `new_string` landed (±3 by default), so the agent can SEE
*what* changed and at what indentation, not just `{status, replacements}`. It is
**show-not-judge** (numbered lines only — no syntax check or validity verdict),
**language-agnostic** (pure line slicing), and bounded (capped height). For
`replace_all` it shows the first changed region; the count is in `replacements`.

### The coarse `file` execution backend (not phase-emittable)

The fine kinds above are the only file ops a phase advertises to (and accepts
from) the LLM. They are dispatched through the unified ToolRegistry, then build
a coarse `FileIROp` (`{kind: "file", op: ...}`) internally and route to the
shared `op_runtime/file.py` backend. That coarse `file` kind — dropped from
`OP_KIND_MODEL_MAP` — is **not** an LLM-emittable Control IR
kind. It survives only as:

- the shared execution backend the fine handlers delegate to, and
- the target of OS-deterministic preprocessor `run_op` steps
  (`{kind: file, op: ...}`), the chat host file methods, and the `reyn memory`
  CLI.

Those non-phase callers also reach extended sub-ops the fine kinds do not
expose — `mkdir`, `move`, `stat`, and `regenerate_index` (used by `reyn memory`
and memory-managing skills via the preprocessor / CLI, never as phase Control
IR).

## `ask_user`

Pauses the phase and asks the user. The OS prints the question, reads stdin, and re-runs the *same phase* with the answer merged into the input as a `user_message` artifact. Visit count does not increment.

```json
{
  "kind": "ask_user",
  "question": "Which model do you want to target?",
  "suggestions": ["light", "standard", "strong"],
  "options": ["light", "standard", "strong"],
  "required": true
}
```

`suggestions` are free-text hints (the user may still type anything). `options` (PR-F3, #2233) is a **closed selectable set** — when non-empty, the frontend renders a **selector** over exactly those answers (empty → free-text input). `required` (default `true`) — when `false`, the user may dismiss without answering.

## `present`

Routes bulk data plus a declarative display template to the user-facing surface
**without the data round-tripping through LLM output tokens**. The offloaded ref
file is already "data file + handle"; `present` joins that handle to a display
template so the bulk bytes reach the user directly. Presenting N rows costs ~0
output tokens; the moment the agent must *transform* the data it pays to read the
ref instead.

**Tier 0** (`ask_user`'s sibling): presenting to the user — the trust root — is
not an exfiltration channel, so there is no output permission gate. The one gate:
`data_ref` read authority resolves **identically to `file.read`** — `present` can
never read more than the agent's file ops can. Unlike `ask_user`, `present` is
**fire-and-continue** — it does NOT pause the run.

```json
{
  "kind": "present",
  "data_ref": ".reyn/cache/tool-results/2026-.../structured.json",
  "blueprint": {
    "component": "table",
    "rows": {"$bind": "/results"},
    "columns": [
      {"header": "Title", "path": "/title"},
      {"header": "Author", "path": "/author"}
    ]
  }
}
```

Fields (exactly one source, exactly one template):

- `data_ref` (str) **XOR** `data_inline` (any) — the data source. `data_ref` is
  any zone-readable path; an offloaded `structured_ref` is **re-hydrated to its
  full value** (not read from the LLM-visible preview) via `file.read` semantics.
  `data_inline` is small data already in the LLM's context.
- `template` (str) **XOR** `blueprint` (object | array) — the display template.
  `template` is a registered presentation name (the registry + fallback chain
  land in a later PR); `blueprint` is an inline declarative component tree.

**Declarative model (v1 catalog — display-only, non-executable by construction).**
A blueprint is a single component node or a list of them (rendered top to bottom).
Catalog components (all read-only): `text` / `markdown` / `code` / `diff` /
`keyvalue` / `table` / `list` / `image`. There are **no interactive components**
(no buttons / forms) in v1. Bindings are expressed structurally as
`{"$bind": "<json-pointer>"}` — an RFC 6901 JSON Pointer **string** (`""` = whole
document); everything else is a literal. `table` / `list` column paths resolve
**row-relative** (relative to each iterated row). The structural gate at op
validation rejects a non-catalog component or a non-path binding (a hard error,
not a soft drop); it is purely structural — leaf-string neutralization is a single
seam in the render layer (below), not at parse.

**Binding semantics.** Path hit → bind. Path miss → **soft-skip** that binding +
record it in `bindings_dropped` (never a hard failure). Type mismatch → coerce (a
scalar into a `table` `rows` slot → a 1-row table) + record. Guard-stripped → a
bound leaf neutralized or size-capped by the presentation-guard is recorded. When
**all** bindings miss, the op reports `all_bindings_missed` (the generic-viewer
fallback signal; the fallback wiring itself lands in a later PR).

**Presentation-guard (output seam).** Runs **unconditionally**, including for
never-ingested data. Every render-leaf string — labels, literal slot values, AND
bound data values — passes through ONE neutralizer, selected by the target
**surface** (a per-surface strategy, so a future web surface slots in without
touching the binding layer). The v1 **terminal** strategy strips ESC / control
sequences (OSC / CSI) and **escapes** (not strips) Rich console markup so `[red]`
renders literally; it does **not** HTML-escape — in a terminal `<div>` is a
harmless literal, and entity-escaping would corrupt `code` / `diff` content (HTML
neutralization is a future web renderer's concern). **Per-binding size caps**
prevent a `/` (root) pointer bound into a `text` component from dumping a whole
file. Neutralization is a transform (the value still renders, inert) — the ref
remains the full-fidelity source.

**Ack (op result)** — the LLM's only feedback, deliberately compact + high-signal:

```yaml
ok: true
bindings_resolved: 3
rows: 500
bindings_dropped:
  - {path: "/results/0/author", reason: path_not_found}
  # reason ∈ {path_not_found, type_mismatch, guard_stripped}
```

`path_not_found` across many rows reads as "template doesn't match this data
shape"; `type_mismatch` as "right path, wrong component"; `guard_stripped` as
"content neutralized by the guard, not a template bug". The LLM self-corrects a
blind presentation for tens of tokens without ingesting the data.

Event emitted: `presented` (P6 audit) — `{data_ref, template, surface, ingested,
bindings_resolved, bindings_dropped, rows}`. `ingested` (`none` | `partial` |
`full`) is **OS-computed** (was the data inline, or does a `read_file` on the ref
appear earlier in the session), never LLM-self-reported. The event carries **refs
+ stats only, never content bytes** (the data is already durable in the ref).

> PR-A scope: the model + binding + guard run against a **null renderer**
> (`surface: ["null"]`) — there is no UI surface yet. The inline-CUI renderer,
> `presentations.yaml` registry + fallback chain, and replay/rewind rendering land
> in later PRs.

## `sandboxed_exec`

Executes `argv` under a declared `SandboxPolicy` via the OS's selected `SandboxBackend`. Replaces `shell` for cases that need (or will need, once `SeatbeltBackend` / `LandlockBackend` land) real isolation enforcement.

```json
{
  "kind": "sandboxed_exec",
  "argv": ["echo", "hello"],
  "network": false,
  "read_paths": ["{{workspace}}"],
  "write_paths": ["{{workspace}}/output"],
  "allow_subprocess": false,
  "env_passthrough": ["PATH"],
  "timeout_seconds": 60,
  "stdin": null
}
```

Fields:
- `argv` (required) — command + arguments. `argv[0]` is the executable.
- `network` (optional, default `false`) — allow outbound network.
- `read_paths` (optional) — filesystem paths the process may read (glob patterns OK).
- `write_paths` (optional) — filesystem paths the process may write.
- `allow_subprocess` (optional, default `false`) — may spawn children.
- `env_passthrough` (optional) — env-var names that pass through (others are stripped).
- `timeout_seconds` (optional, default `60`) — wall-clock cap.
- `stdin` (optional, default `None`) — bytes written to the process's stdin, if any (#2593: the pipeline DSL's `shell` step threads the previous step's pipe-data here as JSON).

**Backend selection**: `get_default_backend()` chooses per platform. On macOS < 26, `SeatbeltBackend` (sandbox-exec SBPL). On Linux ≥ 5.13 with the `sandbox-linux` extra installed, `LandlockBackend` (+ optional seccomp-BPF stack). On other platforms or when the chosen backend is unavailable, falls back to `NoopBackend` (audit-only, no enforcement) — emits a one-line WARN on first use. Override via `reyn.yaml` `sandbox.backend` (`auto` | `seatbelt` | `landlock` | `noop`) and `sandbox.on_unsupported` (`warn` | `error` | `ignore`).

Result fields: `returncode`, `stdout`, `stderr`, `truncated`, `backend`.

Events emitted: `sandboxed_exec_started`, `sandboxed_exec_completed` (P6 audit trail).

## `web_search`

Searches the public web using DuckDuckGo and returns structured results. **Tier 1** — default allow; no permission declaration required. Can be blocked project-wide with `web.search: deny` in `reyn.yaml`.

```json
{
  "kind": "web_search",
  "query": "reyn agent OS site:github.com",
  "max_results": 10,
  "backend": "duckduckgo"
}
```

Fields: `query` (required), `max_results` (optional, default `10`), `backend` (optional, default `"duckduckgo"`; currently the only supported value).

Standard DuckDuckGo search operators are supported in `query`:

- `site:<domain>` — scope results to one domain (e.g. `site:news.ycombinator.com`)
- `"phrase"` — require exact phrase match
- `-term` — exclude results containing `term`

Use operators when the user's intent is site-specific or phrase-anchored; plain keywords work otherwise. Results are returned as a list of `{title, url, snippet}` objects under `results`.

## `web_fetch`

Fetches a single URL and returns its text-extracted content. **Tier 1** — default allow; no permission declaration required. Typically used after `web_search` to read a result page in detail. Block with `web.fetch: deny` in `reyn.yaml`; pre-approve silently with `web.fetch: allow`.

```json
{
  "kind": "web_fetch",
  "url": "https://example.com/article",
  "prompt": "extract the key findings",
  "max_length": 50000
}
```

Fields: `url` (required), `prompt` (optional hint describing what to extract — informational for the LLM, not executed by the OS), `timeout` (optional, default `30` seconds), `max_length` (optional, default `50000` characters).

HTML responses are text-extracted (scripts, styles, and non-content tags stripped). If the content exceeds `max_length`, it is truncated and `truncated: true` appears in the result. Non-HTML responses are returned as-is.

## `mcp`

Calls a tool on a configured MCP server. Requires the server to be declared in `reyn.yaml` under `mcp.servers:` **and** listed in the skill's `permissions.mcp` frontmatter block.

```json
{
  "kind": "mcp",
  "server": "filesystem",
  "tool": "read_text_file",
  "args": {"path": "README.md"}
}
```

Fields: `server` (required — must match a key under `mcp.servers:` in `reyn.yaml`), `tool` (required — tool name as advertised by the server's `tools/list` response), `args` (optional, default `{}`).

> **Advertised name.** Phases advertise this op to the LLM under the chat-tool
> name `call_mcp_tool`; the OS aliases it back to the `mcp` kind at the parse
> boundary. `mcp` remains the canonical kind in `OP_KIND_MODEL_MAP` and on the
> dispatched op.

The OS resolves the server's transport (`stdio`, `http`, or `sse`), dispatches via `MCPClient`, and returns the tool result. Every call emits `mcp_called`, `mcp_completed`, and (on failure) `mcp_failed` events.

See [concepts/tools-integrations/mcp.md](../../concepts/tools-integrations/mcp.md) for server configuration, transport options, and the security model.

## `mcp_read_resource`

Reads one resource (or a resolved resource-template URI) from a configured MCP server. #2597 slice ②a (resources consumption) — gated by the **same** `permissions.mcp` axis as `mcp` (call_tool): a resource read returns external, potentially sensitive server-authored content, so it is permission-gated identically to a tool call.

```json
{
  "kind": "mcp_read_resource",
  "server": "filesystem",
  "uri": "file:///README.md"
}
```

Fields: `server` (required — must match a key under `mcp.servers:` in `reyn.yaml`), `uri` (required — a resource URI as advertised by the server's `resources/list`, or a resolved `resources/templates/list` template).

> **Advertised name.** Phases advertise this op to the LLM under the chat-tool
> name `read_mcp_resource`; the OS aliases it back to the `mcp_read_resource`
> kind at the parse boundary — same pattern as `mcp`/`call_mcp_tool`.

The OS resolves the server's transport, dispatches via `MCPClient.read_resource` (gated on the server's negotiated `resources` capability — see `require_capability` in `mcp/client.py`), and returns `{"contents": [...]}`. Every call emits `mcp_resource_read`, `mcp_resource_read_completed`, and (on failure) `mcp_resource_read_failed` events.

**Discovery is NOT gated.** `list_mcp_resources` / `list_mcp_resource_templates` (the chat-tool names for `MCPClient.list_resources` / `list_resource_templates`) mirror `list_mcp_tools`: no `control-ir` op kind, no permission gate — pure discovery, routed directly through `MCPGateway` from the router host adapter. Only the content-returning read is a gated op kind, matching the existing `mcp` (call_tool) vs. discovery (`list_tools`) split.

`resources/subscribe` + `resources/updated` push notifications are `mcp_subscribe_resource` / `mcp_unsubscribe_resource` below (#2597 slice ②b).

## `mcp_subscribe_resource` / `mcp_unsubscribe_resource`

Subscribe to (or cancel a subscription to) server-pushed `notifications/resources/updated` for one resource URI on a configured MCP server. #2597 slice ②b — the async push event-source: MCP's `resources/subscribe` is a **state-sync/watch** mechanism, not a message queue — the server pushes a thin "this URI changed" signal (no payload), and the OS re-reads (`mcp_read_resource` / `read_mcp_resource`) to see the new content.

```json
{"kind": "mcp_subscribe_resource", "server": "filesystem", "uri": "file:///README.md"}
```

```json
{"kind": "mcp_unsubscribe_resource", "server": "filesystem", "uri": "file:///README.md"}
```

Fields (both kinds): `server` (required), `uri` (required — a resource URI as advertised by `resources/list`).

Gated by the **same** `permissions.mcp` axis as `mcp` / `mcp_read_resource` (subscribing is a stateful action against the server). Gated ALSO on the server's negotiated `resources.subscribe` sub-capability — distinct from the coarser `resources` capability `mcp_read_resource` gates on: a server may support reading resources without supporting subscriptions to them (`MCPClient.subscribe_resource` fails fast with `MCPCapabilityError` if the server didn't advertise `resources.subscribe=True` at connect).

**Persistent connection required.** A subscription is only meaningful on a HELD (session-lifetime) MCP connection — the subscribed-URI set is tracked in-memory on `MCPConnectionService` (runtime-only, no WAL: a subscription carries no data of its own, so it is fully re-establishable and matches the gen-store runtime-only-state invariant). An ephemeral session (whose per-call `MCPClientPool` closes the connection immediately after the op returns) refuses both ops with a clear error rather than silently accept a subscription that can never observe a push.

**Reconnect re-subscribes automatically.** A transport-death reconnect (the same F1 healing path `mcp`/`mcp_read_resource` use) opens a fresh `mcp.ClientSession`, which starts with no subscriptions of its own — `MCPConnectionService` re-issues `subscribe_resource` for every URI still tracked for that server immediately after the fresh connection opens, so a subscription survives a dropped transport transparently.

**The push notification itself is an EventLog event, not a `control_ir_results` value.** When the server sends `notifications/resources/updated {uri}`, `reyn.mcp.message_handler.ReynMCPMessageHandler.on_resource_updated` emits an `mcp_resource_updated` event (`server`, `uri`) onto the session's `EventLog` — asynchronously, independent of any op call. This slice deliberately stops at the EventLog: wiring `mcp_resource_updated` into the hook dispatcher is a later (hooks-arc) slice. Re-reading subscribed resources on reconnect to catch updates missed while disconnected (a resync-READ, distinct from the re-**subscribe** above) is also a follow-up, not this slice.

Advertised to the LLM under the chat-tool names `subscribe_mcp_resource` / `unsubscribe_mcp_resource` — same alias pattern as `mcp`/`call_mcp_tool`.

## `mcp_get_prompt`

Fetches one rendered prompt (its messages) from a configured MCP server. #2597 slice ②c (prompts consumption) — gated by the **same** `permissions.mcp` axis as `mcp` (call_tool) / `mcp_read_resource`: a rendered prompt returns external, potentially sensitive server-authored content, so it is permission-gated identically.

```json
{
  "kind": "mcp_get_prompt",
  "server": "filesystem",
  "name": "summarize",
  "arguments": {"style": "brief"}
}
```

Fields: `server` (required — must match a key under `mcp.servers:` in `reyn.yaml`), `name` (required — prompt name as advertised by the server's `prompts/list` response), `arguments` (optional, default `{}` — rendering arguments matching the prompt's declared `arguments` schema).

> **Advertised name.** Phases advertise this op to the LLM under the chat-tool
> name `get_mcp_prompt`; the OS aliases it back to the `mcp_get_prompt` kind
> at the parse boundary — same pattern as `mcp`/`call_mcp_tool` and
> `mcp_read_resource`/`read_mcp_resource`.

The OS resolves the server's transport, dispatches via `MCPClient.get_prompt` (gated on the server's negotiated `prompts` capability — see `require_capability` in `mcp/client.py`), and returns `{"description": str | None, "messages": [...]}` — each message a flattened `PromptMessage` (`role` + `content`). Every call emits `mcp_prompt_get`, `mcp_prompt_get_completed`, and (on failure) `mcp_prompt_get_failed` events.

**Discovery is NOT gated.** `list_mcp_prompts` (the chat-tool name for `MCPClient.list_prompts`) mirrors `list_mcp_resources`/`list_mcp_tools`: no `control-ir` op kind, no permission gate — pure discovery, routed directly through `MCPGateway` from the router host adapter. Only the content-returning get is a gated op kind, matching the existing `mcp`/`mcp_read_resource` vs. discovery split.

**Prompts have no subscribe concept.** Unlike resources (`mcp_subscribe_resource`/`mcp_unsubscribe_resource`), MCP's `prompts` capability has no server-push notification for a specific prompt's content changing — only the coarser `notifications/prompts/list_changed` (bridged to an EventLog event by `reyn.mcp.message_handler.ReynMCPMessageHandler.on_prompt_list_changed`, independent of this op kind). There is no `mcp_subscribe_prompt` to build.

## `mcp_install`

Installs an MCP server from `registry.modelcontextprotocol.io` into the project's config.
**Phase-only** (not available from the router). Requires `permissions.mcp_install: true`
in the skill's frontmatter **and** user approval.

```json
{
  "kind": "mcp_install",
  "server_id": "io.github.modelcontextprotocol/server-filesystem",
  "scope": "local",
  "env_overrides": {"GITHUB_TOKEN": "ghp_..."}
}
```

Fields:
- `server_id` (required) — registry identifier (e.g. `"io.github.foo/bar-mcp"`).
- `scope` (optional, default `"local"`) — config tier to write to:
  - `"local"` → `<project>/.reyn/config.yaml`
  - `"project"` → `<project>/reyn.yaml`
  - `"user"` → `~/.reyn/config.yaml`
- `env_overrides` (optional) — pre-supplied secret env values; skip interactive prompt
  for keys present here.

Handler lifecycle:
1. Fetches `server.json` via `RegistryClient`
2. Checks runtime command availability (`npx` / `uvx` / `docker` / `dnx`)
3. Gates via `PermissionResolver.require_file_write` (= `.reyn/mcp.yaml`) + `require_http_get` (= registry host); the legacy `require_mcp_install` bool-axis gate has been removed
4. Prompts for `isSecret=true` env vars via `intervention_bus`; each `save_secret` routes through `PermissionResolver.require_secret_write` (= Phase 6 wildcard `"*"` covers the runtime-determined key set)
5. Writes `mcp.servers.<name>` to the target scope config file
6. Emits `mcp_server_installed` event (P6) — key names only, no values

> **Removed ops.** The `embed` and `index_write` control-IR ops were removed.
> Embedding + index writing are now done **provider-direct** inside
> `reyn.api.safe.embed_index.embed_and_index()` (a safe-mode `python` step
> streams its own chunks into it — the bundled `index_docs` / `index_events`
> chunkers were removed along with the stdlib skills that wrapped them) and
> inside the `recall` op (query embedding). The `EmbeddingProvider` and
> `SqliteIndexBackend` primitives are unchanged — only the run-op wrappers and
> bundled chunkers are gone. Nothing emits `kind: embed` / `kind: index_write`
> anymore.

## `skill_install`

Registers a skill (from a local directory or a git/GitHub source URL) into the
project's `skills.entries` config. Two tool surface verbs converge on the same
`op_runtime/skill_install.py` handler: `skill_management__install_local` (local
path) and `skill_management__install_source` (git/URL, PR-D, #2548).

Local-path example:
```json
{
  "kind": "skill_install",
  "path": "skills/my-skill",
  "name": "my-skill"
}
```

Source/git example:
```json
{
  "kind": "skill_install",
  "source": "https://github.com/user/skill-repo",
  "name": "my-skill"
}
```

Subdir convention (mirrors Terraform): `"https://github.com/user/repo//skills/my-skill"`
selects the `skills/my-skill` subdirectory inside the cloned repo.

Fields:
- `path` (required when `source` is absent) — path to the skill directory (containing
  `SKILL.md`) or the direct path to the `SKILL.md` file. May be absolute or
  project-root-relative. When pointing at a directory the handler appends `/SKILL.md`.
  Ignored when `source` is set.
- `source` (optional, PR-D) — git or GitHub URL. The handler shallow-clones the repo
  to `.reyn/skills/<name>/`. Subdir inside the repo is specified via `//` separator.
  Requires `http.get: [{host: <source_host>}]` in the caller's permission declaration.
- `scope` (optional, default `".reyn/config/skills.yaml"`) — retained for
  forward compat; currently unused (all installs write to `.reyn/config/skills.yaml`).
- `name` (optional) — config key override. When absent the handler resolves:
  frontmatter `name:` field → directory basename → repo/subdir basename (in that order).
  The resolved name is **sanitized to a single safe path component** (`[A-Za-z0-9._-]`;
  no `/`, `\`, `..`, or leading `.`) — an unsafe name (from caller `op.name` OR third-party
  SKILL.md frontmatter) is **rejected** with `status="error"`, never used to build a path.

Handler lifecycle (source path inserts steps 0a–0d before step 1):
0. **Source path only**: (a) Gate `require_http_get` for the source host. (b) Sanitize the
   candidate name (`_safe_skill_name`) + verify the clone destination is contained under
   `.reyn/skills/` (`_contained_under`) — refuse before any filesystem mutation if either
   fails (path-traversal → arbitrary-rmtree guard). Shallow-clone repo to
   `.reyn/skills/<candidate_name>/`. (c) Locate `SKILL.md` in root or subdir.
   (d) After the frontmatter name is resolved AND sanitized, containment-check + rename
   clone dir if name ≠ candidate.
1. Resolve `SKILL.md` path (dir → `<dir>/SKILL.md` or direct file)
2. Read `SKILL.md` and `split_frontmatter()` — extract `name` and `description`
3. Apply `op.name` override when set
4. Threat-scan description via `content_guard.scan_for_threats(scope="strict")` — block on
   blocking-severity match (source path: removes clone on block)
5. Gate via `PermissionResolver.require_file_write` (= `.reyn/config/skills.yaml`)
6. Write `skills.entries.<name>` to `.reyn/config/skills.yaml` with
   `{path, description, enabled: true, auto_invoke: true}` (+ `source: <url>` when set)
7. Call `record_config_generation` (recovery-core: truncation-surviving snapshot, #2259 / CLAUDE.md gate)
8. Emit `skill_installed` event (P6 audit trail)
9. Request hot-reload via `get_active_hot_reloader().request_reload(source="skill_install")`

Result fields: `status` (`"installed"` / `"blocked"` / `"error"`), `name`, `path`,
`description`, `config_path`, `source` (empty string for local installs).

Events emitted: `skill_install_threat_match`, `skill_install_threat_blocked` (threat scan),
`skill_installed` (P6 on success).

## `pipeline_install`

Registers a pipeline (from a local DSL file or a git/GitHub source URL) into the
project's `pipelines.entries` config. Two tool surface verbs converge on the same
`op_runtime/pipeline_install.py` handler: `pipeline_management__install_local` (local
path) and `pipeline_management__install_source` (git/URL). Mirrors `skill_install`
as closely as possible, reusing its generic path-safety + sandboxed git-clone helpers
verbatim (`_safe_skill_name` / `_contained_under` / `_parse_source_spec` /
`_source_host` / `_shallow_clone` / `_read_yaml` / `_write_yaml` /
`_resolve_project_root` carry no skill-specific logic).

Local-path example:
```json
{
  "kind": "pipeline_install",
  "path": "pipelines/hello.yaml"
}
```

Source/git example:
```json
{
  "kind": "pipeline_install",
  "source": "https://github.com/user/pipeline-repo"
}
```

Subdir convention (mirrors Terraform, same as `skill_install`):
`"https://github.com/user/repo//pipelines/my-pipeline"` selects the
`pipelines/my-pipeline` subdirectory inside the cloned repo.

Fields:
- `path` (required when `source` is absent) — the direct path to the pipeline's
  `*.yaml` DSL file. Unlike `skill_install`, there is no directory-or-file
  resolution — a pipeline registration is always exactly one file. For a source
  install, `path` (when set) selects the DSL file relative to the repo root/subdir;
  when omitted, the repo root/subdir must contain exactly one `*.yaml` file.
- `source` (optional) — git or GitHub URL. The handler shallow-clones the repo
  to `.reyn/pipelines/<name>/`. Subdir inside the repo is specified via `//` separator.
  Requires `http.get: [{host: <source_host>}]` in the caller's permission declaration.
- `scope` (optional, default `".reyn/config/pipelines.yaml"`) — retained for
  forward compat; currently unused (all installs write to `.reyn/config/pipelines.yaml`).
- `name` (optional) — when set, MUST match the DSL's own declared `pipeline:` name
  exactly; a mismatch is refused (`status="error"`). Unlike `skill_install`'s `name`
  (which freely renames the registered key), a pipeline's declared `pipeline:` name
  is ALWAYS the resolution key a `call`/`match` step targets — the config entry key
  cannot diverge from it. When `name` is omitted, the config key defaults to the
  DSL's declared name.

Handler lifecycle (source path inserts steps 0a–0d before step 1):
0. **Source path only**: (a) Gate `require_http_get` for the source host. (b) Sanitize the
   candidate name + verify the clone destination is contained under
   `.reyn/pipelines/` — refuse before any filesystem mutation if either
   fails (path-traversal → arbitrary-rmtree guard). Shallow-clone repo to
   `.reyn/pipelines/<candidate_name>/`. (c) Locate the DSL file (`path` selects it, or
   the sole `*.yaml` file in the repo root/subdir). (d) After the declared name is
   resolved AND sanitized, containment-check + rename clone dir if name ≠ candidate.
1. Resolve the DSL file path (local: `op.path` directly; source: the located clone file)
2. Parse via `parse_pipeline_dsl` — a malformed file is refused (`status="error"`), never registered
3. Resolve + validate the registration name: `op.name` (if set) must match the DSL's
   declared `pipeline:` name exactly; a mismatch is refused
4. Threat-scan the pipeline description via `content_guard.scan_for_threats(scope="strict")` — block on
   blocking-severity match (source path: removes clone on block)
5. Gate via `PermissionResolver.require_file_write` (= `.reyn/config/pipelines.yaml`)
6. Write `pipelines.entries.<name>` to `.reyn/config/pipelines.yaml` with
   `{path, description, enabled: true}` (+ `source: <url>` when set)
7. Call `record_config_generation` (recovery-core: truncation-surviving snapshot, #2259 / CLAUDE.md gate)
8. Emit `pipeline_installed` event (P6 audit trail)
9. Request hot-reload via `get_active_hot_reloader().request_reload(source="pipeline_install")`
   (the existing `"pipelines"` seam — `Session._reapply_pipelines` — rebuilds the registry)

Result fields: `status` (`"installed"` / `"blocked"` / `"error"`), `name`, `path`,
`description`, `config_path`, `source` (empty string for local installs).

Events emitted: `pipeline_install_threat_match`, `pipeline_install_threat_blocked` (threat scan),
`pipeline_installed` (P6 on success).

## `index_query`

Semantic similarity search over a single indexed source.

```json
{
  "kind": "index_query",
  "source": "project_docs",
  "query_vector": [0.1, 0.2, ...],
  "top_k": 5,
  "filters": {"path": "docs/concepts"}
}
```

Fields:

- `source` (str, required) — logical source name.
- `query_vector` (list[float], optional) — pre-computed embedding. If `null`, falls back to catalog enumeration (up to `fallback_size_cap` tokens).
- `top_k` (int, default `5`) — number of results to return.
- `filters` (dict[str, str], optional) — metadata key/value filters applied before ranking.
- `fallback_size_cap` (int, default `4096`) — token cap for enumerate fallback when `query_vector` is `null`.

Returns: `{"kind": "index_query", "source": str, "results": [{"text": str, "score": float, "metadata": dict}]}`.

## `recall`

Macro op: embed a query → call `index_query` per source → merge and return top-K results globally. The preferred high-level op for RAG retrieval.

```json
{
  "kind": "recall",
  "query": "How does crash recovery work?",
  "sources": ["project_docs", "api_reference"],
  "top_k": 5,
  "embedding_model": "standard"
}
```

Fields:

- `query` (str, required) — natural-language query to embed and search.
- `sources` (list[str], required) — logical source names to search. Must not be empty.
- `top_k` (int, default `5`) — number of results returned after global merge.
- `filters` (dict[str, str], optional) — forwarded to each `index_query` sub-op.
- `embedding_model` (str, default `"standard"`) — model class forwarded to the `embed` sub-op.

Returns: `{"kind": "recall", "results": [{"text": str, "score": float, "source": str, "metadata": dict}]}`.

Events: `recall_embed_failed` if the embed sub-op fails (query, error).

## `index_drop`

Removes an indexed source entirely — deletes its SQLite backend and manifest entry. **Destructive and irreversible.** Requires `permissions.index_drop: ask` (or explicit `allow`) in skill frontmatter, and triggers a user-approval gate by default.

```json
{
  "kind": "index_drop",
  "source": "project_docs"
}
```

Fields:

- `source` (str, required) — logical source name to drop.

Returns: `{"kind": "index_drop", "source": str, "chunks_dropped": int}`.

Events: `index_dropped` (`source`, `chunks_dropped`).

## `judge_output`

LLM-based output scorer for in-phase evaluation loops. Resolves a `target` dot-path to a value, calls an LLM with the caller-supplied `rubric`, and returns a score (0.0–1.0) plus a pass/fail flag.

```json
{
  "kind": "judge_output",
  "target": "artifact.data.summary",
  "rubric": "Score 0.0-1.0: is the summary concise, accurate, and complete?",
  "threshold": 0.8,
  "on_fail": "transition"
}
```

Fields:
- `target` (str, required): Dot-path to the value being scored (e.g. `"artifact.data.summary"`). Resolved against the current workspace artifact.
- `rubric` (str, required): LLM prompt body. Skill author writes the evaluation criteria. The OS never interprets this content (P7).
- `threshold` (float, optional, default `0.8`): Passing score in `[0.0, 1.0]`.
- `on_fail` (`"transition" | "abort" | "continue"`, optional, default `"transition"`):
  - `"transition"`: LLM picks next phase (existing decision flow).
  - `"abort"`: Abort skill execution.
  - `"continue"`: Score recorded only; no flow change.
- `model` (str | null, optional): Model class override (e.g. `"strong"`). Defaults to the skill's current model.

Returns: `{"kind": "judge_output", "score": float, "passed": bool, "reason": str, "threshold": float, "on_fail": str}`

Audit event: `tool_executed` with `op=judge_output, target, score, passed, threshold, reason` (P6).

**P7 note**: Reyn is rubric-agnostic. The rubric content is part of the skill's authored prompt; the OS only routes it to the LLM without inspection.

## `compact`

Voluntarily compact the conversation/phase history *now*, freeing context
window. The OS injects a **context-size signal** (a `## Context window` header
with the exact-token free window) when the window is filling; the model may
respond by emitting `compact` instead of waiting for the mandatory `retry_loop`
backstop. The op routes to the caller-wired compaction (chat:
`force_compact_now`; phase: `compact_control_ir_results` on-demand seam) and
reports the freed tokens + the free window afterwards, in exact tokens
(unit-aligned with the media load-contract error so "should I compact" and
"what fits now" use the same scale).

```json
{
  "kind": "compact"
}
```

Fields:
- `reason` (str, optional): Short model-supplied rationale for the audit trail. The OS never interprets it.

Returns:
- `status: "ok" | "error"`
- `freed_tokens: int` — exact-token reduction. **Per-axis meaning**: on the **phase** axis this is the real `control_ir_results` shrink. On the **chat** axis it is **~0 by construction** — the router prompt is head+tail *turn*-count bounded (`_build_history_for_router`), so compaction does not shrink the bounded view; it compresses the already-elided middle into a summary bridge. Don't front `freed_tokens` for chat.
- `free_window_after` / `free_window_before: int` — exact-token headroom after / before.
- **Chat-axis compression metric** (the meaningful chat signal; `null` on the phase axis): `summarized_turns: int` (older turns folded into the bridge), `compressed_tokens: int` (their raw token cost), `bridge_tokens: int` (the summary's token cost). The chat value is the `compressed_tokens → bridge_tokens` compression, not `freed_tokens`.
- On error: `error_kind` (`compaction_unavailable` when no compaction context is wired here; `compaction_failed`) + `error`.

**Events**: `compact_op_requested` / `compact_op_completed` (`freed_tokens`, `free_window_after`, + chat-axis `summarized_turns` / `compressed_tokens` / `bridge_tokens`) / `compact_op_failed` / `compact_op_unavailable` (P6). The inner compaction engine emits its own compaction events.

**Permission**: none required (LLM cost only). Voluntary and independent of the involuntary `retry_loop` backstop, which always runs regardless.

**Visibility**: advertised to the LLM (tool / `available_control_ops`) only when the window is filling — paired with the context-size signal — so it is not offered when there is nothing to compact (mirrors the `search_actions` visibility gate). The permission gate stays "allow"; only *when surfaced* is gated.

**Axis scope (chat vs phase)**: the `compact` op is available on **both** axes. On the **chat** axis, it routes to `force_compact_now`; on the **phase** axis, it routes to the `compact_control_ir_results` on-demand seam wired by the phase runtime (in addition to the automatic per-frame compaction that fires regardless). In both cases the OS wires `ctx.compact_now`; the op handler itself is axis-agnostic. Both axes also inject the paired context-size signal so the model knows when to emit `compact`.

## Task ops

First-class trackable work-units. A Task is **opt-in and additive** — the
session model (concurrent / interleaved) is unchanged; a Task is a discrete
handle whose lifecycle is tracked independently. **Completion is an explicit
declaration** (the assignee emits `task.update_status`), never inferred from
session state.

These ops are **term-neutral (P7)**: names + fields are generic; A2A vocabulary
(`contextId`, `TaskState`) maps only at the A2A layer. The op family is gated by
`allowed_ops` (declare `task` for the whole family, or individual `task.*`
kinds); like any op, each is also subject to the per-session contextual gate.

```json
{ "kind": "task.create", "name": "ship-feature" }
{ "kind": "task.create", "name": "sub", "deps": ["<other-id>"] }
{ "kind": "task.create", "name": "bg-sub", "link_type": "background" }
{ "kind": "task.update_status", "task_id": "<id>", "status": "running" }
{ "kind": "task.add_dependency", "task_id": "<id>", "depends_on": "<other-id>" }
{ "kind": "task.abort", "task_id": "<id>" }
```

**Roles.** A Task is owned by two session identities (the per-contextId
routing-key): the **requester** (origin / assigner / disposition notify-target —
the caller of `task.create`, set by the OS, not an op field) and the **assignee**
(the worker session, the single-writer of `status`). Under #2187 backend-master the
assignee is a **rebindable WAL subscription binding** (§27-31), not an immutable field:
it may be **`None` (UNASSIGNED — the pending-assignment queue)** and is changed via
`task.assign` (claim / owner-initiated hand-off / re-queue). On `task.create`: an explicit
`assignee` delegates (or self-assigns); an **owned sub-task** (created while executing a
task — OS-derived ownership, §16) with no `assignee` defaults to the caller (the
decomposition continuation); a **top-level** task with no `assignee` is **UNASSIGNED**
(it waits in the queue until claimed). One session can be the assignee of many Tasks (1 : N).

**Single-writer** is an op-layer CAS `caller session_id == the CURRENT (hydrated) assignee`
(`OpContext.session_id`, threaded by the OS — not an op field, so it cannot be forged),
checked against the rebindable WAL binding; a non-assignee write is rejected. This is
**not** a permission gate (the permission system is resource-scoped, no caller identity at
op-exec). The single-writer invariant (one writer per task) keeps the read-then-check
race-free without a claim token / version.

**Pending-assignment queue (§27-31).** A top-level `task.create` with no `assignee`
produces an **UNASSIGNED** task (no binding, no execute-wake) that sits in the queue —
listed by `task.list status=unassigned`. `task.assign` then binds a session: an UNASSIGNED
task may be **claimed by any session**; an already-assigned task may be **reassigned only by
its current assignee** (owner-initiated hand-off — others request a change via conversation).
Assignment rebinds the WAL subscription, OS-derives the now-startable status
(READY / BLOCKED-by-deps), and wakes the new assignee to execute.

**Role-based op authority (P5).** Each op is gated on the caller's `session_id`:
*assignee-gated* — `update_status` / `heartbeat` / `register_unblock_predicate`;
*requester-gated* — `create` / `add_dependency` / `get` / `abort`; `assign` is
**current-assignee-gated for a reassign but open for an UNASSIGNED claim**. A violation
returns a `role_denied` result.

**`abort` = delete (cooperative-terminal).** `task.abort` is the requester's
remove-op (it absorbs the former `task.archive`): it archives the task **and its
whole sub-tree** (DOWN-cascade, §18). There is **no forced cancel** — the
assignee's in-flight work is rejected by the **terminal-state guard** on
`update_status` at its next write (so no straggler lands, and a sibling task's
work is untouched). This is correct under 1:N (a session owns many tasks) and
needs no cross-session machinery. **UP-notify** (§16): a non-completed terminal
(aborted / failed / cap_exceeded) with **still-alive dependents** notifies the task's
**requester** (the §16 disposition notify-target — the request-owner) to decide recovery.
For an `origin=self` (internal) task the requester's **session is woken** (the slice-7
TaskWaker) so its LLM re-wires the stuck dependents via ordinary task ops (P7 — no
`decision=` vocabulary); for an `origin=external` task the A2A layer routes it to the
external (webhook) channel. The requester is always present, so a **root task is notified
too** (#2107: the prior parent-keyed routing dropped roots). Abort also emits a generic
P6 `task_disposition` event per aborted task (`task_id` / `requester` / `origin` /
`disposition`).

**States (7-state, #2187 §3.4):** `unassigned` (no assignee — the pending-assignment
queue) / `blocked` (deps not all terminal) / `ready` (assigned, startable) / `running` /
`done` / `failed` / `aborted`. Soft-delete (`archived_at`) is an orthogonal retention
marker, not a state.

**Dependency DAG (§13).** `task.add_dependency` and `task.create(deps=[...])` add
depends-on edges through a **shared edge-guard** (completeness): the `depends_on`
task must exist and the edge must not create a cycle. A rejected edge returns a
decision-enabling **error result** (`status="error"`, `error.kind="cycle"` /
`"dep_not_found"`, the offending `edge`, and — for a cycle — the `path`), never a
raised exception. Edge-add is a **pure topology write** — it never flips a task's
status (the requester does not write the assignee's status). A task born with
not-all-completed deps is **OS-derived `blocked`** at create (deps-less tasks keep
their requested status).

**Mutable edges (slice 6-ext).** `task.remove_dependency` drops an edge
(idempotent — a no-op on a missing edge); `task.repoint_dependency` atomically
repoints `from_depends_on` → `to_depends_on`, **cycle-checking the NEW edge BEFORE
any mutation** (a cycle/dangling repoint changes nothing and returns the same
structured error). Both are requester topology writes that go through the same
shared edge-guard, then run the **OS-authority readiness re-derive**.

**Readiness re-derive (OS-authority, P3).** A single primitive derives a task's
readiness over the **pre-run** scheduling states `{pending, ready, blocked}`:
promote `blocked → ready` when all deps are satisfied (incl. a deps-less task —
removing the last dep readies it), re-block `{pending, ready} → blocked` when they
are not. An `in_progress` (the assignee owns the run) or terminal task is left
untouched — this is the single-writer split (the OS schedules pre-run, the
assignee owns the run), and the write **bypasses the assignee CAS** like `abort`.
Completion-recompute (relax → only promotes), `remove` (relax), and `repoint`
(may demote or promote) all share it. A readiness change emits a generic P6
`task_readiness` event.

**Disposition → requester routing (§16, S1 #2134).** When a task reaches a
non-`completed` terminal (`aborted` via `task.abort`, `failed` via
`task.update_status`, or `cap_exceeded` on a per-Task budget cap-hit) and has
**still-alive dependents**, the OS notifies the task's **requester** (the §16
disposition notify-target — the request-owner) to decide recovery — the requester
re-wires via ordinary ops (`repoint` / `remove` / fail / support-self), **not** a
`decision=` vocabulary (P7). If the requester is a **task** (`requester_kind=TASK`
— a task-as-request owns the failed dependent), the OS resolves one hop to that
task's **assignee** (the managing session) before waking. The **requester is always
present** (every task carries one), so a root task is notified too — the prior
`parent_id`-keyed routing silently dropped root-task recovery wakes (#2107). The
**disposition** is carried first-class (in both the P6 `task_dependency_aborted`
event and the requester payload) so a budget `cap_exceeded` is never conflated with
a genuine error `failed` (slice 8). External-origin tasks route via the A2A/webhook
channel rather than an in-session wake.

**Per-Task budget cap (slice 8).** `record_cost(task_id, delta)` accumulates an
LLM call's cost onto the Task's `cost_accum`; when it crosses the Task's
`budget_cap` (an INDEPENDENT cap dimension, enforced alongside the session / daily
caps — the tighter hits first), the OS force-terminates the task (abort-like) and
routes the `cap_exceeded` disposition through the SAME requester-LLM seam — so one
recovery mechanism resolves both a terminal-dependency and a cap-hit. (The
production wiring of the LLM cost recorder to `record_cost` co-lands with the
task-execution engine in a later slice.)

**The TaskWaker (slice 7).** The `OpContext.task_waker` (the OS `TaskWaker`
driver) turns these dispositions into actual session **wakes** via the canonical
`resolve_session → _put_inbox → ensure_session_running` triple: a promoted
dependent (on a predecessor `completed` OR a recovery `repoint`/`remove`) is woken
with a `task_ready` inbox message; a requester (or its managing session) is woken with `task_dependency_aborted`.
Both surface to the woken session's LLM as one router turn (OS-generic inbox kinds,
P7) so it resumes / recovers via ordinary task ops. A **loopless** session (A2A /
MCP, no run-loop) is booted by `ensure_session_running`; a looped one is an
idempotent no-op. This in-process driver is complementary to the cross-process A2A
webhook disposition sweep.

Still landing in later slices: per-task liveness (unblock-predicate / heartbeat
evaluation) — the residual non-terminal-stuck backstop.

---

**Note for contributors:** When adding a new Control IR op kind to `src/reyn/schemas/models.py` and `src/reyn/core/op_runtime/registry.py`, **also add a section here** in the same PR. The reference and the registry must stay in sync — see [CLAUDE.md](https://github.com/tya5/reyn/blob/main/CLAUDE.md) for the rule.

## Where ops are exposed to the LLM

The OS injects available ops into every context frame as `available_control_ops`. Each entry includes a `kind`, a one-line description, and a worked example. The LLM picks ops by matching its intent to descriptions — phase markdown MUST NOT describe op syntax (P8).

## See also

- [events.md](events.md) — events emitted per op kind
- Concepts: principles P8 (principles doc removed)
