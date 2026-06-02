---
type: how-to
topic: files
audience: [human]
---

# Work with local files

Reyn can read files from your project and answer questions about them — no special syntax required. Just describe what you want in plain language.

---

## Before you start: one-time setup

File access goes through the `filesystem` MCP server. Add the following block to your `reyn.yaml` if it is not already there:

```yaml
permissions:
  mcp.filesystem: allow   # skip the per-call prompt

mcp:
  servers:
    filesystem:
      type: stdio
      command: npx
      args: ["-y", "@modelcontextprotocol/server-filesystem", "."]
```

The `.` at the end of `args` sets the server's root to your current directory. Everything inside that directory tree is readable; paths outside it are not.

If you skip the `permissions` line, Reyn will prompt you to approve each read interactively. That works fine in the TUI; it will block in headless environments.

See [How-to: Manage permissions](manage-permissions.md) for the full options.

---

## Reference a single file

Write the file path naturally — Reyn understands both casual descriptions and exact paths:

```
> Summarise the README
> What does pyproject.toml declare as dependencies?
> Explain what src/reyn/runtime.py does
```

The skill resolves the path and reads the file before composing its answer. It tells you which file it actually read at the end of the response.

---

## Reference multiple files

Name more than one file in a single request:

```
> Compare the approach in docs/concepts/runtime/workspace.md and docs/concepts/runtime/events.md
> What's different between src/reyn/models.py and src/reyn/op_runtime/registry.py?
> Read CHANGELOG.md and pyproject.toml and tell me what version we are on
```

Reyn reads up to five files per turn. If a request would need more, break it into follow-up turns.

---

## Ask about a directory

Name a directory and Reyn picks the most relevant entry point — an `__init__.py`, `index.md`, `README`, or similar:

```
> What's in src/reyn/op_runtime/?
> Walk me through what's under docs/concepts/
```

It will not read every file in a large directory; it infers which file to start from. If it picks the wrong one, tell it which file you meant and it will re-read.

---

## Common scenarios

### Summarise documentation

```
> Summarise the philosophy section of docs/concepts/architecture/principles.md
> Give me a one-paragraph overview of docs/guide/for-users/index.md
```

### Understand source code

```
> What does the `ContextFrame` class in src/reyn/models.py do?
> List the public functions in src/reyn/op_runtime/registry.py
> How does src/reyn/runtime.py start a skill run?
```

### Check configuration

```
> What MCP servers are configured in reyn.yaml?
> Does this project have any pre-approved permissions?
```

### Spot differences

```
> What changed between the two versions described in CHANGELOG.md?
> Compare the input schemas in these two artifact YAML files
```

---

## What the skill cannot do

- **Write or modify files** — `read_local_files` is read-only. If you need to edit a file, say so explicitly; the router will pick a different skill.
- **Read files outside the server root** — if you configured the server with `.` as root, paths like `/etc/passwd` or `~/.ssh/config` are outside scope and will return an error. The server enforces this boundary, not Reyn.
- **Read binary files** — the underlying tool is `read_text_file`. Images, compiled artifacts, and other binaries are not supported.

---

## Troubleshooting

**"permission denied" on every read**

Add `mcp.filesystem: allow` to the `permissions:` block in `reyn.yaml` (see [setup](#before-you-start-one-time-setup) above) or answer the interactive prompt with `[y]` during a TUI session.

**"path outside project scope" error**

The filesystem MCP server's root is set when you start it (the final argument in `args`). Paths must be relative to that root. Absolute paths and `../`-escaping paths are rejected by the server.

**Reyn reads the wrong file**

Say which file you meant:

```
> Not that one — I meant src/reyn/op_runtime/registry.py
```

The skill will re-read the correct path.

**No response, or "skill exited empty"**

The `filesystem` server may not be running or may be misconfigured. Check that `mcp.servers.filesystem` is present in `reyn.yaml` and that `npx` is installed (`npx --version`).

---

## See also

- [How-to: Manage permissions](manage-permissions.md) — approve, persist, and revoke filesystem access
- [Getting started: Chat mode](../getting-started/02-chat-mode.md) — the basics of `reyn chat`
