---
type: reference
topic: stdlib
audience: [human, agent]
applies_to: [read_local_files]
---

# `read_local_files`

Read a file from the project (or anywhere the configured `filesystem` MCP server can see) and answer questions about its contents — the canonical example of an MCP-backed stdlib skill.

## When to use

- The user asks about a file by name or path: "What does `pyproject.toml` declare?", "Summarise the licence section of README".
- The router emits `read_local_files` when the request is filesystem-shaped and a configured `filesystem` server is available.

## When NOT to use

- For free-form code search across the project — that's `web_research` or grep-style ops, not single-file reads.
- For files inside `.reyn/` that the OS already reads via default `file.read` permissions — no MCP detour needed.
- For binary files; the underlying tool is `read_text_file`.

## Required setup

### Setup checklist

1. **MCP filesystem server** — add `mcp.servers.filesystem` to `reyn.yaml` (see block below).
2. **Permission pre-approval** — add `mcp.filesystem: allow` to the `permissions:` block in `reyn.yaml`.
   Without this, Reyn prompts interactively on every MCP call; in headless / non-TTY environments
   (CI, piped stdin, dogfood scripts) the prompt cannot be answered and every call returns
   `permission_denied`, causing the skill to exit empty.
3. See [`examples/configs/with-mcp.yaml`](../../../../examples/configs/with-mcp.yaml) for a complete
   working example — copy it to your project root and rename to `reyn.yaml`.

A `filesystem` MCP server MUST be configured in `reyn.yaml` under that exact name.
Paste the block below into your existing `reyn.yaml` (both `mcp` and `permissions` sections are required):

```yaml
permissions:
  mcp.filesystem: allow   # required for headless / non-TTY execution

mcp:
  servers:
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

See [How-to: use an MCP server](../../how-to/use-an-mcp-server.md) for the full setup walkthrough, including installing the `[mcp]` extra.

## Phases

<!-- TODO: confirm phase names once read_local_files lands; the skill is being authored in parallel. -->

| Phase | Purpose |
|-------|---------|
| `read_and_respond` | Entry phase. Resolves the requested path, emits an `mcp` op against `filesystem`, formats the response. May finish with `file_content_response`, or transition for follow-up. |

The phase declares `permissions.mcp: [filesystem]` in its frontmatter.

## Final output: `file_content_response`

| Field | Type | Purpose |
|-------|------|---------|
| `path` | string | The path that was read (as resolved by the server) |
| `content` | string | File contents, or the answer derived from them |
| `summary` | string (optional) | One-paragraph synopsis when the user asked for a summary rather than raw text |

<!-- TODO: confirm exact field set with the parallel implementation agent. -->

## Examples

Sample prompts that route here:

- "Read README.md and tell me what reyn is."
- "What licences are mentioned in `LICENSE`?"
- "Summarise the philosophy section of `docs/en/concepts/principles.md`."

Sample prompts that DO NOT route here:

- "Find all TODO comments in the repo." → broader search; not a single-file read.
- "What's in `.reyn/events.jsonl`?" → handled by default `file.read`, no MCP.

## Source

[`src/stdlib/skills/read_local_files/`](https://github.com/<org>/reyn/blob/main/src/stdlib/skills/read_local_files/)

## See also

- [Concepts: MCP](../../concepts/mcp.md) — how reyn integrates the protocol
- [How-to: use an MCP server](../../how-to/use-an-mcp-server.md) — the quickstart this skill exercises
- [Reference: `reyn.yaml` § MCP servers](../config/reyn-yaml.md#mcp-servers)
