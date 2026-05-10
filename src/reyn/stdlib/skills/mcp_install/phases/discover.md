---
type: phase
name: discover
input: user_message
role: mcp_installer
can_finish: true
allowed_ops: [mcp_install, ask_user]
preprocessor:
  - type: python
    module: ./registry_fetch.py
    function: fetch_server_for_install
    into: data.registry
    output_schema:
      type: object
      properties:
        server_id:
          type: string
          description: Exact registry identifier if found, or empty string
        candidates:
          type: array
          description: Search candidates if server_id is ambiguous
          items:
            type: object
            properties:
              name:        {type: string}
              description: {type: string}
              repo_url:    {type: string}
            required: [name, description, repo_url]
        source:
          type: string
          description: "direct | search | not_found | error"
        query:
          type: string
      required: [server_id, candidates, source, query]
---

The MCP registry has already been queried by the OS preprocessor. Use the data in
`data.registry` — do NOT call web_fetch or search yourself.

## Step 1 — Read preprocessor result

`data.registry.source` tells you what happened:
- `"direct"` — user gave an exact server_id; `data.registry.server_id` is set.
- `"search"` — user gave a description; `data.registry.candidates` has results.
- `"not_found"` — query returned no results.
- `"error"` — registry unreachable.

## Step 2 — Determine server_id and scope

If `source == "direct"`:
  - Use `data.registry.server_id` directly.

If `source == "search"` and candidates exist:
  - Present the candidates to the user (using ask_user op) and get their selection.
  - Use the selected candidate's `name` as server_id.

If `source == "not_found"` or `source == "error"`:
  - Finish with status="error" explaining what happened. Do not attempt web_fetch.

Scope: default to `"local"` unless the user explicitly requested `"project"` or `"user"`.

## Step 3 — Emit mcp_install op and finish

Once server_id and scope are determined, emit a single `mcp_install` op:

```json
{"kind": "mcp_install", "server_id": "<server_id>", "scope": "<scope>"}
```

If `data.extra_args` is present and non-empty, include it in the op:

```json
{"kind": "mcp_install", "server_id": "<server_id>", "scope": "<scope>", "extra_args": ["--server", "pyright"]}
```

Wait for the op result in the next act turn. Then finish with the `mcp_install_result`
artifact using the data from the op result:
- If op status is `"ok"`: set status="installed", include server_name and installed_path.
- If op status is `"error"` or `"denied"`: set status="error", include the error message.

Do NOT describe the internal op format in your instructions — the OS injects the
available ops via `available_control_ops`.
