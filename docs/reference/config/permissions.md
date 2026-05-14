---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml, skill.md, phases/*.md]
---

# Permissions

reyn's permission system gates access to file paths, shell, MCP tools, named tools, and Python preprocessor steps. Defaults are conservative; anything outside the defaults requires either a phase-level declaration plus user approval, OR a project-wide pre-approval in `reyn.yaml`.

## Default grants (no declaration needed)

| Op | Scope |
|----|-------|
| `file.read` / `file.glob` / `file.grep` | Any path under the project root (CWD). |
| `file.write` / `file.edit` / `file.delete` | Only under `<CWD>/.reyn/` or `<CWD>/reyn/`. |

Anything outside these defaults must be declared.

## Phase declarations (`permissions:` in phase frontmatter)

```yaml
---
type: phase
name: example
input: user_message
permissions:
  shell: true
  mcp: [my_server]
  tool: [web_search]
  file:
    read:
      - path: ~/notes
        scope: recursive
    write:
      - path: /tmp/output
        scope: just_path
  python:
    - module: stats
      function: compute
      mode: safe
      timeout: 30
---
```

### `shell`

`true` to enable the `shell` Control IR op for this phase. Off by default.

Even with `shell: true`, the runtime requires `--allow-shell` at startup; otherwise the op emits `shell_not_allowed`.

### `mcp`, `tool`

List of MCP server names / named tool ids the phase may call.

### `file.read` / `file.write`

For paths outside the default zones. Each entry has:

- `path` — absolute, or relative to CWD. `~` is expanded.
- `scope` — `just_path` (this exact path) or `recursive` (this path and everything below it).

`file.write` covers `write`, `edit`, and `delete` ops.

### `python`

Per-(module, function) declarations for `python` preprocessor steps. See [`reference/dsl/preprocessor.md`](../dsl/preprocessor.md).

- `module`, `function` — must match the corresponding preprocessor step.
- `mode` — `safe` (sandboxed) or `unsafe` (no AST sandbox; needs `--allow-untrusted-python`).
- `timeout` — wall-clock seconds before the parent SIGKILLs the child. Default `30`.

## Approval flow (interactive)

When a phase declares a non-default permission, reyn shows a single startup prompt:

```
[approval] my_skill/file.write needs:
  /tmp/output (just_path)

  [y] allow this run only
  [j] persist approval for this exact path + skill
  [r] persist approval for the parent dir (recursive) + skill
  [N] deny
```

Persistent choices land in `.reyn/approvals.yaml` keyed by `<skill>/<op>/<path>` (with a trailing `/` for recursive grants). External skills cannot reuse another skill's approvals — keys are skill-scoped to prevent privilege escalation.

## Project-wide pre-approval (`reyn.yaml`)

```yaml
permissions:
  shell: allow
  file.write: allow         # grants ALL write-class ops for ALL skills
  python:
    safe: allow             # auto-approve all safe-mode python steps
    unsafe: allow           # also requires --allow-untrusted-python at runtime
    allowed_modules:
      - math
      - statistics
      - mypackage
```

Use `allow` only when the project is trusted. `ask` (the default) prompts; `deny` rejects.

## Non-interactive runs (CI, eval)

`reyn eval` runs non-interactively — there is no prompt. Approvals must be pre-arranged either in `reyn.yaml` or `.reyn/approvals.yaml` (e.g. by running the target skill once interactively first).

## Inspecting and revoking

```bash
reyn permissions list             # show saved approvals
reyn permissions revoke <key>     # remove an approval
```

## See also

- [reyn-yaml.md](reyn-yaml.md) — full project config
- [state-dir.md](state-dir.md) — `.reyn/approvals.yaml` location
- [Reference: skill.md](../dsl/skill-md.md) — declaring permissions
- [Reference: control-ir](../runtime/control-ir.md) — which ops need permissions
