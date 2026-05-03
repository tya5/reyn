---
type: phase
name: read_and_respond
input: read_plan
role: file_synthesiser
can_finish: true
allowed_ops: [mcp]
max_act_turns: 3
---

Read each path in the plan via the `filesystem` MCP server, then compose
a single natural-language answer that uses what you found.

## Inputs

- `input_artifact.data.paths` — paths planned by `decide_files`
- `input_artifact.data.reason` — one-line focus hint from `decide_files`
- `control_ir_results` — populated by the OS after each act turn with
  the results of the `mcp` ops you emitted in the previous turn

## Strategy — at most 3 act turns

```
act 1: emit one mcp op per path  →  control_ir_results returned
act 2: (optional) emit follow-up reads if a result was empty / errored
        and a sibling path is obviously the right one
act 3: decide turn — final response
```

Most prompts finish in **act 1 → decide**. The extra turns are slack
for narrow recovery, not for fan-out. Do not re-read paths that already
returned successfully.

## How to invoke the MCP server

Each `mcp` op MUST use `server: filesystem` and the canonical filesystem
MCP tool name `read_text_file`:

```json
{
  "kind": "mcp",
  "server": "filesystem",
  "tool": "read_text_file",
  "args": {"path": "src/reyn/runtime.py"}
}
```

The OS gates the call through the per-server permission, executes it,
and returns a result of shape:

```json
{
  "kind": "mcp", "status": "ok",
  "server": "filesystem", "tool": "read_text_file",
  "content": "<file body as text>",
  "raw": {...}
}
```

When `status: "error"` (server not configured, path outside server root,
permission denied, file missing, etc.), the `error` field describes
what went wrong. Treat the path as unread and continue.

## Composing the final answer

When you have read what you needed (or determined you cannot read more
usefully), emit the decide turn with a `file_content_response`:

- `response` — answer the user's actual question. Use file contents to
  ground specific claims; quote sparingly. If some reads errored, name
  the paths that failed rather than fabricating their contents.
- `files_read` — the paths whose `mcp` result was `status: "ok"`.
  Empty list is acceptable when every read failed.

## Constraints

- Match the user's language (Japanese in → Japanese out, etc.).
- Do **not** invent file contents. If a read failed, say so.
- Do **not** emit ops in the decide turn — ops belong to act turns only.
- Do **not** loop indefinitely re-trying failed paths; one corrective
  re-read is the absolute maximum.
