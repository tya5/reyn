---
type: reference
topic: config
audience: [human, agent]
applies_to: [reyn.yaml]
---

# Permissions

reyn's permission system gates access to file paths, exec (argv-only process execution — `#3226` renamed `shell` -> `exec`), MCP tools, named tools, and Python preprocessor steps. Defaults are conservative; anything outside the defaults needs a project-wide declaration in `reyn.yaml`'s `permissions:` block, and — for an access the declared set does not cover — a just-in-time operator approval.

## Default grants (no declaration needed)

The readable / writable path sets live in `permissions.file.read` and
`permissions.file.write`. When you write nothing, the **schema default** applies —
the default is part of the configuration's definition, not a rule hidden inside
the runtime gate, so the enforcement side and the side that tells the model what
it may touch read the same answer.

| Axis | Schema default | Covers |
|----|-------|-------|
| `file.read` (also `file.glob` / `file.grep`) | `<zone-root>` | The zone root and everything below it. |
| `file.write` (also `file.edit` / `file.delete`) | `<zone-root>/.reyn` | The state dir, minus the protected carve-outs (`.reyn/approvals.yaml`/`.reyn/approvals.jsonl` — legacy and live approval stores, #5153/#5173 — and the `.reyn/config/` + `.reyn/state/` recovery-core prefixes, which are mutated only through their dedicated ops). |

`<zone-root>` is a **symbol**, not a literal path: it is the zone anchor the
entry point supplies — the workspace base dir under `reyn chat` / `reyn web`,
the project root under `reyn pipe` / plugin install / registry bootstrap, the
in-container repo root under a container backend. A literal path could not be
written as a default because its value is not known until the process starts.

### The three ways to write the set

| Config | Meaning |
|---|---|
| unset | the schema default above |
| `file.read: deny` | the empty set — nothing is readable, and the just-in-time prompt is suppressed too |
| `file.read: [path, …]` | exactly that set. It **replaces** the default; it is not unioned with it, because the list *is* the permission. An entry is a bare path or `{path, scope}`; a `reyn.yaml` scope entry is `recursive` unless it says `just_path`. |
| `file.read: allow` | no path restriction on the axis |

A path outside the resolved set is not simply refused: the just-in-time layer
still asks the operator when a request bus is available (chat / interactive
runs) and denies when there is none (headless / eval). Config is the standing
set; JIT is the per-access extension of it.

## Declaring permissions

**The only place a permission is declared is `reyn.yaml`'s `permissions:`
block** — see [Project-wide pre-approval](#project-wide-pre-approval-reynyaml)
below for its shape. There is no `skill.md` frontmatter `permissions:` key: an
earlier version of this page documented one, and a parser for it that was
never built (`docs/reference/config/permissions.md`'s own drift, #5863) — the
production read path is the single call to `PermissionDecl.from_dict` in
`session.py`, and the dict it is handed there is `PermissionResolver._config`,
which is `reyn.yaml`'s `permissions:` block, never anything parsed out of a
`skill.md` file. If you are looking at a `skill.md` (or workflow/phase file)
with a `permissions:` key in its frontmatter, that key is inert — nothing
reads it.

## Web ops

`web_search` is **Tier 1**: passes through by default without any declaration. Restrict project-wide via `permissions.web.search: deny`.

`web_fetch` is unified under the `http.get` axis (same per-host gate as `safe.http`). The chat router injects `http.get: [{host: "*"}]` so LLM-driven fetches keep working with per-host prompts replacing the old per-URL prompts. Legacy `permissions.web.fetch: allow / deny` config keys remain honored as backward-compat aliases during the migration window.

```yaml
permissions:
  web.search: deny   # block all web_search ops
  web.fetch: deny    # legacy alias — overrides http.get wildcard, raises immediately
  web.fetch: allow   # legacy alias — pre-approves any host (= equivalent to ALWAYS for all hosts)
```

This differs from Tier 2-3 ops (`shell`, `mcp`) which require an explicit declaration in `reyn.yaml`'s `permissions:` block before the op is even attempted.

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

Persistent choices land in `.reyn/approvals.jsonl` (an append-only ledger — #5153) keyed by `<skill>/<op>/<path>` (with a trailing `/` for recursive grants). External skills cannot reuse another skill's approvals — keys are skill-scoped to prevent privilege escalation.

For `file.read`/`file.write`, a key match alone no longer settles it (#5042): the approved path's own identity is bound the first time it's used and re-checked on every later use, so deleting the approved target and recreating a different object at the same path re-prompts instead of silently inheriting the old grant — see [Concepts: permission model](../../concepts/runtime/permission-model.md).

## Project-wide pre-approval (`reyn.yaml`)

```yaml
permissions:
  shell: allow
  file.write: allow         # grants ALL write-class ops for ALL skills
  python:
    safe: allow             # auto-approve all safe-mode python steps
    allowed_modules:
      - math
      - statistics
      - mypackage
```

Use `allow` only when the project is trusted. `ask` (the default) prompts; `deny` rejects.

## Granting an MCP server permission

MCP's per-server gate (`permissions.mcp`) has two independent grant surfaces — pick the one that matches when you're deciding:

1. **`reyn.yaml` / `reyn.local.yaml` — declare up front, before any prompt fires.**

   ```yaml
   permissions:
     mcp:
       github: allow   # grant this MCP server's tools project-wide
   ```

   A flat blanket form also works (`permissions.mcp: allow` grants every server), but the per-server dict form above is the one to reach for when you trust one server and not others. This is a config file, edited by the operator — it takes effect the next time reyn loads config, no running session needed.

2. **`.reyn/approvals.jsonl` — the saved-approvals ledger the interactive prompt writes to.**

   When a chat session hits an undeclared MCP server, it prompts (`y` / `j` / `r` / `N` — see "Approval flow" above); choosing a persistent option (`j`/`r`) appends a `mcp.<server>: true` record under that skill's key to `.reyn/approvals.jsonl` (an append-only ledger, folded on read — last record per key wins). Don't hand-edit this file: it's a durable, fsync'd-per-line record of every decision ever made, not a plain snapshot you author from scratch. A pre-#5153 `.reyn/approvals.yaml` snapshot is migrated into it once, on first touch, if the ledger doesn't already exist — after that `approvals.yaml` is inert history, never read again.

Both surfaces feed the same runtime check (`require_mcp` — see "Runtime gate: `permissions.mcp`" in [the MCP concept doc](../../concepts/tools-integrations/mcp.md)); `reyn.yaml` is the declarative up-front grant, `.reyn/approvals.jsonl` is the session's own memory of interactive answers.

**`reyn pipe run`'s default:** running a pipeline via `reyn pipe run` (a one-shot, non-interactive CLI command — see below) auto-grants `permissions.mcp` for every MCP server already present in the merged MCP config (`.reyn/config/mcp.yaml` plus `reyn.yaml`/`reyn.local.yaml`'s `mcp.servers`). The gate itself is unchanged — an MCP server that is NOT configured there still denies, and an explicit `deny` you've set for a specific server (or a blanket `mcp: deny`) is never auto-overridden. This only changes the pipe-run *default* for servers you've already configured; it does not touch `reyn chat`'s own interactive prompt.

## Non-interactive runs (CI)

`reyn run-once` runs non-interactively — there is no prompt. Approvals must be pre-arranged either in `reyn.yaml` or `.reyn/approvals.jsonl` (e.g. by running the agent once interactively first). `reyn pipe run` is the same non-interactive model, EXCEPT for MCP servers — see "Granting an MCP server permission" above for the pipe-run-specific auto-grant of already-configured servers.

## Inspecting and revoking

```bash
reyn permissions list             # show currently-approved keys (the ledger folded)
reyn permissions revoke <key>     # append an approved=False record — the grant stays in history
```

## See also

- [reyn-yaml.md](reyn-yaml.md) — full project config
- [state-dir.md](state-dir.md) — `.reyn/approvals.jsonl` location

- [Reference: control-ir](../runtime/control-ir.md) — which ops need permissions
