---
type: how-to
topic: config
audience: [human]
applies_to: [reyn.yaml, .reyn/approvals.yaml, phases/*.md]
---

# Manage permissions

**Goal:** Grant the right capabilities to a skill without over-broadening trust, and inspect / revoke approvals after the fact.

## Three places to set permissions

| Layer | Lives in | Granularity |
|-------|----------|-------------|
| Phase declaration | Phase frontmatter | Per phase + per (op, path) |
| Saved approvals | `.reyn/approvals.yaml` | Per (skill, op, path) |
| Project-wide pre-approval | `reyn.yaml` `permissions:` | Per op kind |

The defaults are conservative; the rest is opt-in. See the [permission model concept](../concepts/permission-model.md) for the why.

## Declare in a phase

```yaml
---
type: phase
name: writeout
input: report
permissions:
  shell: false
  file:
    write:
      - path: /tmp/output
        scope: just_path
  python:
    - module: stats
      function: compute
      mode: pure
      timeout: 30
---
```

`scope: just_path` matches the exact path; `recursive` matches a directory and all descendants.

## Approve at startup

When the skill needs something not in the defaults, the runtime prompts:

```
[approval] my_skill/file.write needs:
  /tmp/output (just_path)

  [y] allow this run only
  [j] persist for this exact path + skill
  [r] persist for the parent dir (recursive) + skill
  [N] deny
```

`j` and `r` write to `.reyn/approvals.yaml`.

## Pre-approve project-wide

```yaml
# reyn.yaml
permissions:
  shell: allow
  file.write: allow
  python:
    pure: allow
    trusted: allow      # also requires --allow-untrusted-python at runtime
```

`allow` removes the prompt entirely. `ask` (default) prompts. `deny` rejects.

## Inspect saved approvals

```bash
reyn permissions list
```

Output groups entries by skill, then by op kind:

```
  [my_skill]
    ✓ write  /tmp/output  (just_path)
    ✓ read   ~/notes      (recursive)
```

## Revoke

```bash
reyn permissions revoke my_skill/file.write//tmp/output
reyn permissions clear     # remove all (asks for confirmation)
```

## Eval mode

`reyn eval` is non-interactive. Pre-arrange every approval the target skill needs:

- run the target once with `reyn run` and persist via `[j]` or `[r]`, OR
- pre-approve in `reyn.yaml`.

Without prior approval the eval case is reported as not-finished.

## See also

- [Reference: permissions](../reference/config/permissions.md)
- [Reference: reyn.yaml](../reference/config/reyn-yaml.md)
- [Reference: state-dir](../reference/config/state-dir.md)
- [Concepts: permission model](../concepts/permission-model.md)
