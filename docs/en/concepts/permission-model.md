---
type: concept
topic: architecture
audience: [human, agent]
---

# Permission model

reyn's permission system gates four kinds of capability: file paths, shell, MCP tool calls, and Python preprocessor steps. The defaults are conservative; anything beyond them must be declared by the skill **and** approved by the user (or pre-approved in `reyn.yaml`).

## Three layers, in order

```
┌──────────────────────────────┐  always allowed; nothing to declare
│  defaults (read-only project)│
└──────────────────────────────┘
             ↓ if skill needs more
┌──────────────────────────────┐  declare in phase frontmatter; user approves
│  phase declarations          │  approval persists to .reyn/approvals.yaml
└──────────────────────────────┘
             ↓ if you trust the project broadly
┌──────────────────────────────┐  reyn.yaml: permissions.<key>: allow
│  project-wide pre-approval   │  bypasses the prompt for that capability
└──────────────────────────────┘
```

### Layer 1: defaults

Read/glob/grep anywhere under the project root. Write/edit/delete only under `.reyn/` or `reyn/`. No shell, no MCP, no Python.

### Layer 2: phase declarations

A phase that needs something outside the defaults declares it in its frontmatter. At skill startup, the runtime shows a single approval prompt:

```
[approval] my_skill/file.write needs:
  /tmp/output (just_path)

  [y] allow this run only
  [j] persist for this exact path + skill
  [r] persist for the parent dir (recursive) + skill
  [N] deny
```

Persistent choices land in `.reyn/approvals.yaml` keyed by `<skill>/<op>/<path>`. Keys are skill-scoped — one skill's approval doesn't leak to another.

### Layer 3: project-wide pre-approval

`reyn.yaml` can pre-grant capabilities project-wide:

```yaml
permissions:
  shell: allow
  file.write: allow
  python:
    pure: allow
    trusted: allow
```

Use sparingly — `allow` removes the prompt entirely.

## Non-interactive runs

`reyn eval` runs without prompts. Approvals must be in place beforehand: either pre-approved in `reyn.yaml` or persisted to `.reyn/approvals.yaml` from a prior interactive run.

This is the same trust model: the eval doesn't get to decide what's safe; you do, in advance.

### reyn.local.yaml for operator-local pre-approval

For dogfood automation, CI runs, or any non-interactive scripted use, the natural
mechanism is `reyn.local.yaml` — a gitignored operator-personal override of `reyn.yaml`
(layer 3 project-wide pre-approval, scoped to the local machine).  Add:

```yaml
permissions:
  file:
    read: allow
  python:
    pure: allow
    trusted: allow
```

This grants project-wide pre-approval for the local environment without affecting
committed `reyn.yaml` or production users.  Interactive TTY runs elsewhere still see
startup_guard prompts as documented.

## Why skill-scoped keys

Approvals are keyed by skill, not globally. If skill A asks "can you write to `/tmp/foo`?", granting it doesn't grant skill B the same access.

The reason is composition safety. Skill A might be trusted; skill A invoking sub-skill B (via `run_skill`) doesn't transitively grant B's permissions. B has to ask for its own.

## What the permission system is NOT

- **Not a Linux capability sandbox.** A Python step in `mode: trusted` runs as the same user; reyn doesn't sandbox the kernel.
- **Not a secret keeper.** Don't put credentials in approvals.yaml or rely on permissions to hide environment variables.
- **Not protection against the user.** If you `permissions: shell: allow` in reyn.yaml, you've authorized shell. The system is protecting against accidental capability creep, not user intent.

## See also

- [Reference: permissions](../reference/config/permissions.md) — full schema
- [Reference: reyn.yaml](../reference/config/reyn-yaml.md) — `permissions:` key
- [Reference: state-dir](../reference/config/state-dir.md) — `.reyn/approvals.yaml`
- [How-to: manage permissions](../how-to/manage-permissions.md)
