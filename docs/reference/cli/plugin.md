---
type: reference
topic: cli
audience: [human, agent]
applies_to: [reyn plugin]
---

# `reyn plugin`

Install/uninstall a self-contained reyn plugin bundle (ADR 0064 §3.9, P3). A **thin
adapter** over the SAME typed op the LLM tool / slash surfaces use — this command
builds a real `ToolContext` and dispatches through `invoke_tool` exactly the way a
live chat-router LLM tool call does, so the composite permission declaration and the
`{kind:git}` run-code trust gate live in exactly one place
(`tools/plugin_management_verbs.py`), never re-derived here.

## Synopsis

```
reyn plugin install builtin <NAME> [--name <install-name>] [--project <path>] [--non-interactive]
reyn plugin install local <PATH> [--name <install-name>] [--project <path>] [--non-interactive]
reyn plugin install git <URL> [--name <install-name>] [--project <path>] [--non-interactive]
reyn plugin uninstall <NAME> [--project <path>]
```

## Subcommands

### `install builtin|local|git`

The typed source **kind is the subcommand itself**, never a form-sniffed string —
mirrors how `reyn mcp install` distinguishes `--source` forms structurally.

| Kind | Argument | What it does |
|---|---|---|
| `builtin <NAME>` | reyn's own shipped plugin name | Installs from `src/reyn/builtin/plugins/<NAME>/`. |
| `local <PATH>` | a local plugin directory | Promotes/installs your own author→test→promote working copy. |
| `git <URL>` | a remote git URL | **Highest RCE trust risk** (ADR 0064 §3.10 item 3) — installs arbitrary remote code. |

Common flags (all three kinds):

| Flag | Notes |
|---|---|
| `--name INSTALL_NAME` | Override the install directory / registry-provenance name (default: the manifest's own declared name). |
| `--project PATH` | Project root containing `reyn.yaml`. Defaults to the closest ancestor with one, or cwd. |
| `--non-interactive` | Suppress interactive prompts (for CI use). A `{kind:git}` install **always fails closed** non-interactively — the run-code trust decision cannot be pre-made (ADR 0064 §3.10 item 3). |

```bash
reyn plugin install builtin my-shipped-plugin
reyn plugin install local ./my-plugin-dir --name my-plugin
reyn plugin install git https://github.com/user/reyn-plugin   # requires a live interactive approval
```

On success, prints the tool result as JSON:

```bash
$ reyn plugin install builtin my-shipped-plugin
Installing plugin (kind=builtin): my-shipped-plugin

{
  "status": "ok",
  "data": {"...": "..."}
}
```

Failure exits non-zero: **2** for a permission denial or a tool-reported error,
**1** for an unexpected exception.

### `uninstall <NAME>`

Removes a previously installed plugin: drops its registry entries and removes the
`~/.reyn/plugins/` copy. `NAME` is the plugin's install name — the name `install`
used or returned, not necessarily the manifest's own declared name if `--name`
overrode it.

```bash
$ reyn plugin uninstall my-plugin
Uninstalling plugin: my-plugin

{
  "status": "ok",
  "data": {"...": "..."}
}
```

`uninstall` only deletes, so it never needs the `{kind:git}` run-code trust gate —
there is no `--non-interactive` flag for it; the command is inherently safe to run
unattended.

## Permission model

Every install runs through a real `PermissionResolver`/`Workspace`/`EventLog` built
against the resolved `--project` root (or the closest `reyn.yaml` ancestor), loading
that project's own `permissions:` config — not the invoking shell's cwd. The gates
enforced (in `tools/plugin_management_verbs.py`, not re-derived here):

- `require_file_write` on `~/.reyn/plugins/`
- `require_http_get` (for a `local`/`git` source that fetches anything)
- `require_plugin_git_run_code_trust` for `{kind:git}` — fails closed whenever
  either the intervention bus is absent or the resolver isn't interactive (i.e.
  `--non-interactive`, or no tty), never both required to deny.

## Related

- [`reyn mcp`](mcp.md) — the `--source` structural-discrimination convention this
  command's typed subcommand kind mirrors
- [`reyn audit`](audit.md) — the static scan that flags a risky MCP/delegation
  configuration a plugin might introduce
