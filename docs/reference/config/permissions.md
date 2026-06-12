---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml, skill.md, phases/*.md]
---

# Permissions

reyn's permission system gates access to file paths, shell, MCP tools, named tools, and Python preprocessor steps. Defaults are conservative; anything outside the defaults requires either a skill-level declaration plus user approval, OR a project-wide pre-approval in `reyn.yaml`.

## Default grants (no declaration needed)

| Op | Scope |
|----|-------|
| `file.read` / `file.glob` / `file.grep` | Any path under the project root (CWD). |
| `file.write` / `file.edit` / `file.delete` | Only under `<CWD>/.reyn/` or `<CWD>/reyn/`. |

Anything outside these defaults must be declared.

## Skill declarations (`permissions:` in skill.md frontmatter)

Phase-level `permissions:` was removed. All permission declarations belong in `skill.md` frontmatter — see [skill-md.md](../dsl/skill-md.md#permissions-skill-level). Phases inherit whatever the skill declares.

```yaml
---
type: skill
name: example
entry: main
final_output: result
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
  http.get:
    - host: api.github.com           # specific host: startup_guard prompts once, runtime silent
    - host: "*"                      # wildcard: runtime per-host 4-layer prompt for any URL
  secret.write:
    - GITHUB_TOKEN                   # specific key, or
    - "*"                            # wildcard for runtime-determined keys (= user-prompt is the gate)
  python:
    - module: stats
      function: compute
      mode: safe
      timeout: 30
---
```

### `shell`

`true` to enable the `shell` Control IR op for this phase. Off by default.

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
- `mode` — `safe` (sandboxed) or `unsafe` (no AST sandbox; needs `--allow-unsafe-python` at runtime).
- `timeout` — wall-clock seconds before the parent SIGKILLs the child. Default `30`.

### `http.get`

Per-host HTTP allowlist for `reyn.safe.http.*` (skill-internal) AND for `web_fetch` (LLM-driven) — both surfaces share one axis.

- **Specific host** (`http.get: [{host: "api.github.com"}]`) — `startup_guard` prompts once per `<skill, host>`; runtime is silent after approval. Same model as `file.write` outside the default zone.
- **Wildcard** (`http.get: [{host: "*"}]` or `["*"]`) — host set is unknown at write-time (= LLM picks at runtime); the 4-layer prompt fires inside `require_http_get` at the actual host gate; ALWAYS / NEVER persists per host.
- **No declaration** — legacy `web.fetch` compat fallback with `DeprecationWarning` until the migration window closes.

`reyn.safe.http` (subprocess path) accepts only specific hosts; wildcard requires the async `web_fetch` op route.

### `secret.write`

Per-key allowlist for `~/.reyn/secrets.env` writes (= called by the `mcp_install` op handler when persisting `isSecret` env vars).

- **Specific key** (`secret.write: ["GITHUB_TOKEN"]`) — authorises that exact env-var name.
- **Wildcard** (`secret.write: ["*"]`) — runtime-determined key set (= mcp_install reads `isSecret` env vars from the registry response). The operator's per-value prompt at op-execution time is the actual security gate.

## Web ops

`web_search` is **Tier 1**: passes through by default without any declaration. Restrict project-wide via `permissions.web.search: deny`.

`web_fetch` is unified under the `http.get` axis (same per-host gate as `safe.http`). The chat router injects `http.get: [{host: "*"}]` so LLM-driven fetches keep working with per-host prompts replacing the old per-URL prompts. Legacy `permissions.web.fetch: allow / deny` config keys remain honored as backward-compat aliases during the migration window.

```yaml
permissions:
  web.search: deny   # block all web_search ops
  web.fetch: deny    # legacy alias — overrides http.get wildcard, raises immediately
  web.fetch: allow   # legacy alias — pre-approves any host (= equivalent to ALWAYS for all hosts)
```

This differs from Tier 2-3 ops (`shell`, `mcp`) which require an explicit declaration in `skill.md` before the op is even attempted.

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
    unsafe: allow           # also requires --allow-unsafe-python at runtime
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
