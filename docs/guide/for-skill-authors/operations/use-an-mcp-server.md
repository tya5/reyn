---
type: how-to
topic: integration
audience: [human]
applies_to: [reyn.yaml, mcp.servers, read_local_files]
---

# Use an MCP server

**Goal:** Wire an [MCP](../../../concepts/mcp.md) server into reyn and call it from a skill. We'll use the official `filesystem` server and the stdlib `read_local_files` skill as the worked example — substitute any other server (`git`, `github`, `fetch`, `brave-search`, …) by changing the `command` and `args`.

## When to use

- You want a skill to read or search files outside the workspace's default zone.
- You want to plug in any of the [official MCP servers](https://github.com/modelcontextprotocol/servers) without writing custom code.
- You're authoring a new MCP-backed skill and need a known-good baseline to copy.

## 1. Install the server

### Recommended: `reyn mcp install`

For servers listed in the MCP registry, use `reyn mcp install`. It handles the server binary, config, credentials, and the permission gate automatically:

```bash
# Discover servers first (optional)
reyn mcp search "filesystem"

# Install — handles everything in one step
reyn mcp install io.github.modelcontextprotocol/server-filesystem
```

After install, the `mcp.servers.filesystem` entry is already in your config (in `reyn.local.yaml` by default, or `reyn.yaml` if you pass `--scope project`). Skip to step 3.

The same flow is also driveable from a `reyn chat` session via the `mcp__search_registry` / `mcp__install_registry` verbs — see [`reyn mcp` CLI reference § Chat-side equivalents](../../../reference/cli/mcp.md#chat-side-equivalents).

### Advanced: manual config

If a server is not in the public registry, or you want full control over the config, add it manually. Smoke-test the server standalone first — you don't want to debug both the server and the integration at once:

```bash
# Run it manually; it should print server info and wait on stdin
npx -y @modelcontextprotocol/server-filesystem .
```

Press `Ctrl-C` once you see it accept the JSON-RPC handshake. (Each MCP server has its own install command — check the server's README. `pip`, `cargo`, and bare binaries are common alternatives.)

## 2. Configure in `reyn.yaml` (manual path)

Add an `mcp.servers:` block. Pick a short, kebab-or-snake-case name (`filesystem` is conventional) — this is the name your skill will declare in `permissions.mcp` and emit in `mcp` ops.

```yaml
# reyn.yaml
mcp:
  servers:
    filesystem:
      type: stdio
      command: npx
      args:
        - "-y"
        - "@modelcontextprotocol/server-filesystem"
        - "."           # root the server can see; use absolute paths to widen
```

For a server that requires credentials, store the value in `~/.reyn/secrets.env` and reference it as `${VAR}`:

```bash
# Store once
reyn secret set GITHUB_PERSONAL_ACCESS_TOKEN

# Then in reyn.yaml:
#   env:
#     GITHUB_PERSONAL_ACCESS_TOKEN: ${GITHUB_PERSONAL_ACCESS_TOKEN}
```

For HTTP servers swap to `type: http` with `url:` and `headers:` — see [Reference: reyn.yaml § MCP servers](../../../reference/config/reyn-yaml.md#mcp-servers).

## 3. Install reyn's MCP extra

MCP support ships as an optional dependency to keep the minimum install lean.

```bash
pip install -e ".[mcp]"
```

This pulls in the official `mcp` Python SDK, which reyn uses internally for transport. <!-- TODO: confirm extra name (`[mcp]`) once PR32 lands; pyproject.toml may bundle it differently. -->

## 4. Run the example skill

The `read_local_files` stdlib skill is the canonical caller. From a `reyn chat` session:

```bash
reyn chat
```

```
> Read README.md and summarise the philosophy section.
```

What you should see, in order:

1. The router picks `read_local_files` (filesystem-style requests are in its positive examples).
2. The first call to the `filesystem` server prompts for approval:

   ```
   [approval] read_local_files/mcp.filesystem needs:
     MCP server: 'filesystem'

     [y] allow this run only
     [j] persist for this exact path + skill
     [r] persist for the parent dir (recursive) + skill
     [N] deny
   ```

   Pick `j` if you want a persistent approval; `y` to allow once.
3. The skill emits an `mcp` op (`tool: read_text_file`, `args: {path: "README.md"}`), the OS dispatches it via stdio, the server returns the file content.
4. The skill replies with a prose summary of the section you asked about.

## 5. Verify via events

Every MCP call is audit-tracked. Tail the event log:

```bash
reyn events tail
```

You should see, per call:

```
mcp_called      server=filesystem tool=read_text_file args={"path":"README.md"}
mcp_completed   server=filesystem tool=read_text_file is_error=false
```

Or grep the raw log:

```bash
grep '"mcp_' .reyn/events.jsonl | tail -n 5
```

`mcp_failed` shows up instead of `mcp_completed` when the server returns a transport or protocol error.

## Troubleshooting

**`MCP server 'filesystem' is not configured.`** The `mcp.servers.filesystem` block is missing or misnamed. Confirm with `cat reyn.yaml` (or `cat reyn.local.yaml` if installed with `--scope local`); remember the name the skill uses (`filesystem`) must match the key in the config.

**`MCP server 'filesystem' not declared in phase permissions.`** The phase's frontmatter is missing `permissions.mcp: [filesystem]`. Open the phase file and add it. This is the runtime gate, not a config issue.

**Approval prompt appears every run.** You answered `y` (one-shot) instead of `j` / `r`. Re-run and pick `j` to persist, or pre-approve project-wide in `reyn.yaml`:

```yaml
permissions:
  mcp:
    filesystem: allow
```

**Server crashes immediately.** Run the `command` + `args` manually (step 1 of the manual path) — it should accept stdin without exiting. If it fails standalone, fix the install before re-running reyn. The crash is reported as `mcp_failed` with the underlying error.

**`MCP config references undefined environment variable: ${TOKEN}`.** A `${VAR}` reference in the config didn't resolve. Run `reyn secret set TOKEN` to store the value, or export the variable in your shell. Missing vars expand to empty string and warn rather than fail.

**Permission prompts on first install.** This is expected — reyn gates server additions through the standard list axes (`file.write` on `.reyn/mcp.yaml`, `http.get` on the registry host). Select `j` (just this path) or `r` (recursive) to persist. For non-interactive CI runs, pass `--non-interactive` and set `permissions.file.write: allow` + `permissions.web.fetch: allow` in your config beforehand. The legacy `mcp_install: ask | allow | deny` key still works during the migration window (= emits a `DeprecationWarning`).

**`reyn events tail` shows no `mcp_called`.** The skill never reached the `mcp` op — check its phase log to see whether the LLM emitted it. A common cause is the LLM picking `file.read` (default capability, project-scoped) instead of `mcp` because the path was inside the project; that's correct behaviour, not an error.

## See also

- [Concepts: MCP](../../../concepts/mcp.md) — protocol overview, transport choice, security model
- [Concepts: secret handling](../../../concepts/secret-handling.md) — credential storage and `${VAR}` interpolation
- [Reference: `reyn mcp`](../../../reference/cli/mcp.md) — `install`, `set-secret`, and other subcommands
- [Reference: `reyn secret`](../../../reference/cli/secret.md) — managing credentials
- [Reference: `reyn.yaml` § MCP servers](../../../reference/config/reyn-yaml.md#mcp-servers) — full schema
- [How-to: manage permissions](../../for-users/manage-permissions.md) — pre-approval, revoke, eval mode
